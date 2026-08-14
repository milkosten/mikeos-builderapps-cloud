"""Offline tests for phase 32 — the per-project WORKSPACE (the shared work-tracker).

Five properties, each chosen because breaking it is silent and expensive:

1. **The taxonomy is not hardcoded.** `kind` and `status` are free text everywhere — schema,
   API models, tool. This is the lesson `assistants.role` already learned; a CHECK constraint
   or a pydantic Enum sneaking in later would turn "file this as a risk" into a 422 and the
   agent would quietly stop filing anything.

2. **A key is scoped to exactly one project, and the miss is 404.** The whole tenancy model.
   A 403 would confirm the other tenant exists; a missing check would hand it their board.

3. **No credential is baked into the runtime image.** The key arrives per beat in the
   container environment. An image is pulled, copied and shared.

4. **Every mutation writes an event.** The audit trail is the feature — "who moved this to
   done, a human or an agent?" is the question the user actually has.

5. **The pipeline drives the board.** open -> in_progress -> done, and a SKIPPED feature
   becomes `blocked` WITH its reason, not a silently missing row.

    python3 tests/test_phase32.py
"""
import ast
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS, FAIL = [], []


def check(name, cond, why=""):
    (PASS if cond else FAIL).append((name, why))


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


MIGRATION = read("migrations/012_workspace.sql")
STORE = read("server/workspace_store.py")
API = read("server/workspace_api.py")
RUNTIME = read("server/assistant_runtime.py")
PIPELINE = read("server/harness/pipeline.py")
WS = read("assistant-runtime/ws")
SKILL = read("assistant-runtime/skills/workspace/SKILL.md")
DOCKERFILE = read("assistant-runtime/Dockerfile")
BEAT = read("assistant-runtime/beat.py")


# ---------------------------------------------------------------------------
def test_taxonomy_is_open():
    """`kind`/`status` must be free text from the disk up. Three layers, three ways to
    accidentally close it: a CHECK in SQL, an Enum in pydantic, a whitelist in the tool."""
    lowered = MIGRATION.lower()
    check("the schema puts NO check constraint on kind/status",
          "check (" not in lowered and "check(" not in lowered,
          "a CHECK is how `kind` stops being extensible; 006 learned this for `role`")
    check("the migration says out loud that the taxonomy is open",
          "FREE TEXT" in MIGRATION and "never an enum" in MIGRATION.lower(),
          "the next person to touch this must read the rule before adding a constraint")

    tree = ast.parse(API)
    enums = [n for n in ast.walk(tree)
             if isinstance(n, ast.ClassDef)
             and any(getattr(b, "id", getattr(b, "attr", "")) == "Enum" for b in n.bases)]
    check("the API defines no Enum for kind/status", not enums,
          "a pydantic Enum would 422 an unknown kind — the same mistake in a new place")
    check("the API's own docstrings call kind/status free text",
          "FREE TEXT" in API and "Free text" in API)
    check("the defaults are conventions, not validation",
          "DEFAULT_KINDS" in STORE and "conventions" in STORE.lower()
          and "DEFAULT_KINDS" not in API.split("def ")[0].replace("W.DEFAULT_KINDS", ""),
          "the vocabularies exist as hints for a client, never as a gate")
    check("the list endpoint returns kinds/statuses that INCLUDE what actually exists",
          "sorted(c[\"by_kind\"].keys())" in API,
          "an invented kind must appear in the UI's grouping with no deploy")
    check("the tool's help teaches the conventions but calls them free text",
          "FREE TEXT" in WS and "simply starts existing" in WS)


# ---------------------------------------------------------------------------
def test_key_is_scoped_and_the_miss_is_404():
    check("a key resolves to exactly ONE project",
          "SELECT project_id FROM builderapps.workspace_keys WHERE key_sha=$1" in STORE)
    check("the key table is keyed by project (one key per project)",
          "project_id   text PRIMARY KEY" in MIGRATION)
    check("a key used against another project 404s, never 403s",
          "owner_pid != project_id" in API and "status_code=404" in API
          and "status_code=403" not in API,
          "403 confirms the other tenant exists; 404 tells the caller nothing")
    check("the key is looked up by HASH, never by decrypting every row",
          "_sha(key)" in STORE and "key_sha" in STORE)
    check("the plaintext key is never stored",
          "key_enc" in MIGRATION and "key_plain" not in MIGRATION)
    check("an empty or wrong-prefix key can never match",
          'if not key or not key.startswith("wsk_")' in STORE)
    check("every read path is project-scoped",
          STORE.count("project_id=$") >= 8 and "WHERE id=$1 AND project_id=$2" in STORE)
    check("the key-reveal route is OWNER only, not reachable with the key itself",
          "_owner_only(project_id, request)" in API
          and "workspace_key" in API)

    # …and the ownership check on the human path is the same one the rest of the API uses.
    check("the human path checks project ownership",
          "store.get_project(project_id, user_id)" in API)


def test_attribution_cannot_be_faked():
    check("an assistant's identity comes from its SERVER-minted token, not a header",
          "x-assistant-token" in API and "A.get_by_token(tok)" in API)
    check("a token from a DIFFERENT project cannot name an actor here",
          'str(a.get("project_id")) == project_id' in API)
    check("a mere name hint is recorded as `agent`, never as `assistant`",
          'W.Actor("agent", "agent", hint' in API,
          "the trail must not overstate what it knows about who wrote something")
    check("an assistant acting through the control plane is named from its ROW",
          "_ws_actor" in RUNTIME and 'assistant.get("name")' in RUNTIME)


# ---------------------------------------------------------------------------
def test_no_key_in_the_image():
    check("the runtime image copies the tool but no credential",
          "COPY ws /usr/local/bin/ws" in DOCKERFILE
          and "WORKSPACE_API_KEY=" not in DOCKERFILE,
          "an image is pulled, copied and shared; a key inside one is a key in every hand")
    check("the Dockerfile says WHY there is no key in it, and how to verify",
          "NO KEY IS BAKED IN" in DOCKERFILE and "entrypoint sh" in DOCKERFILE)
    check("the scheduler injects the key per beat",
          'cmd += ["-e", f"WORKSPACE_API_KEY={ws_key}"]' in RUNTIME)
    check("the key is minted lazily so older projects are not stranded",
          "W.ensure_key(project_id)" in RUNTIME and "ensure_key" in STORE)
    check("a tracker outage cannot stop a beat",
          "this beat runs without `ws`" in RUNTIME)
    check("the key is redacted out of logs and beat records",
          'r"wsk_[A-Za-z0-9_\\-]{8,}"' in RUNTIME)
    check("the tool refuses to run without the per-beat environment",
          "WORKSPACE_API_KEY / PROJECT_ID are not set" in WS and "inside a beat" in WS)
    check("the workspace skill is only offered when the key actually arrived",
          'endswith("/workspace") and not WORKSPACE_API_KEY' in BEAT,
          "a skill that tells an agent to run a tool that will fail is worse than no skill")


# ---------------------------------------------------------------------------
def test_every_mutation_is_audited():
    for fn in ("create_item", "update_item", "add_comment", "add_link"):
        src = STORE.split(f"async def {fn}(")[1].split("\nasync def ")[0]
        check(f"{fn} writes an event", "add_event(" in src)
    check("an event records the actor as a triple (id, kind, name)",
          "actor, actor_kind, actor_name" in STORE and "actor_kind" in MIGRATION)
    check("a no-op patch writes NO event",
          'if new == (before.get(col) or ""):' in STORE,
          "a trail full of `done -> done` teaches people to stop reading it")
    check("the event trail distinguishes a human from a specific assistant",
          "human | assistant | pipeline" in STORE or "human" in MIGRATION)
    check("GET item returns comments + events + links in ONE call",
          '"comments"' in STORE and '"events"' in STORE and '"links"' in STORE
          and "async def full_item" in STORE,
          "phase 33 DMs carry only an item id; the recipient must act without more calls")
    check("closing a status stamps closed_at, reopening clears it",
          '"closed_at=" + ("now()" if new_status in CLOSED_STATUSES else "NULL")' in STORE)


# ---------------------------------------------------------------------------
def test_pipeline_drives_the_board():
    check("the backlog is seeded as feature items",
          "_ws_seed_backlog" in PIPELINE and 'kind="feature"' in PIPELINE)
    check("a build in flight shows as in_progress",
          '_ws_status(ctx.project_id, idx + 1, "in_progress")' in PIPELINE)
    check("a built feature shows as done",
          '_ws_status(ctx.project_id, idx + 1, "done"' in PIPELINE)
    check("a SKIPPED feature becomes a visible blocked item WITH its reason",
          '_ws_status(ctx.project_id, idx + 1, "blocked", note=reason)' in PIPELINE,
          "this is the payoff: 'the pipeline quietly dropped a feature' stops being "
          "invisible to the user")
    check("the skip note reaches the item body, not only the trail",
          "**Blocked:** " in STORE and "body + add" in STORE)
    check("items are keyed so a RESUMED run updates instead of duplicating",
          "upsert_by_ext_key" in STORE and 'f"build_{i + 1:02d}"' in PIPELINE)
    check("a resume never rewrites a status backwards",
          "STATUS IS LEFT ALONE" in STORE)
    check("runtime QA findings are filed as bugs",
          "_ws_bug" in PIPELINE and 'kind="bug"' in PIPELINE)
    check("the tracker can never take a build down",
          PIPELINE.count("workspace") > 0
          and "never fail a build on the tracker" in PIPELINE
          and "best-effort" in PIPELINE.lower())
    check("a new project gets its key at birth",
          "workspace key" in PIPELINE and "W.ensure_key(ctx.project_id)" in PIPELINE)


# ---------------------------------------------------------------------------
def test_the_agents_can_actually_reach_it():
    check("every assistant SEES the board every beat, deterministically",
          'ctx["workspace"]' in RUNTIME and "open_items" in RUNTIME,
          "a capability only the LLM remembers to use is a capability that never fires")
    check("only live items are sent (a finished feature is history, not context)",
          "W.CLOSED_STATUSES" in RUNTIME)
    check("writing to the board is offered to EVERY assistant, ungated",
          "workspace_add" in RUNTIME and "with no capability gate" in RUNTIME,
          "the read-only assistants are exactly the ones whose whole output is findings")
    check("both workspace acts are dispatched",
          '"workspace_add", "workspace_new"' in RUNTIME
          and '"workspace_update", "workspace_comment"' in RUNTIME)
    check("the coding agent is told about `ws` in its grounding, not only in a skill",
          "the project's shared workspace" in BEAT)
    check("the skill has the frontmatter Pi's loader requires",
          SKILL.startswith("---\nname: workspace\ndescription: ")
          and len(SKILL.split("description: ")[1].split("\n")[0]) <= 1024)
    check("the skill tells the agent to LOOK before it files",
          "ws list" in SKILL and "already filed" in SKILL)
    check("`done` is defined as verified, not as written",
          "means you verified it" in SKILL)


# ---------------------------------------------------------------------------
def test_house_rules():
    check("no reserved keyword as a column",
          "from_val" in MIGRATION and "to_val" in MIGRATION
          and " from text" not in MIGRATION.lower() and " to text" not in MIGRATION.lower())
    check("the migration is idempotent",
          MIGRATION.count("IF NOT EXISTS") >= 9)
    check("timestamps go out as ISO-8601 strings",
          "isoformat()" in STORE,
          "epoch ms into a cloud is the 'HTTP 200, zero rows' bug class")
    check("SQL is parameterized — no f-string interpolation of a VALUE",
          "$1" in STORE and "%s" not in STORE
          and 'f"%{' not in STORE.replace('f"%{q[:200]}%"', ""))
    check("a write is verified, never assumed",
          "returned no row" in STORE)
    check("list/search are bounded",
          "MAX_LIST" in STORE and "min(int(limit" in STORE)
    check("the tool is stdlib-only (it runs where there is no pip)",
          "import requests" not in WS and "urllib.request" in WS)
    check("the tool separates a refusal from a tool failure",
          "OK, REFUSED, TOOL_ERROR = 0, 1, 2" in WS)


def main():
    test_taxonomy_is_open()
    test_key_is_scoped_and_the_miss_is_404()
    test_attribution_cannot_be_faked()
    test_no_key_in_the_image()
    test_every_mutation_is_audited()
    test_pipeline_drives_the_board()
    test_the_agents_can_actually_reach_it()
    test_house_rules()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name, why in FAIL:
        print(f"  FAIL {name}" + (f": {why}" if why else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
