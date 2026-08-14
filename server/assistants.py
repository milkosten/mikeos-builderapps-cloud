"""Per-project AI assistants (phase 29) — model, capabilities, templates, data access.

An assistant is a **closed-loop agent bound to one project**: SOUL.md persona, a heartbeat
every `interval_minutes`, a granted capability set, and one `assistant_beats` row per beat
(`perceive -> reason -> act -> remember`). It is the MikeOS app-agent contract brought inside
a builderapps project, where the "device" is the project: its repo, its live app, its
backlog, its QA results, its bill.

Two rules this module exists to hold:

**1. Roles are OPEN-ENDED.** `role` is free text. A "Security assistant", an "Expense
management assistant" and a "Product Owner" are the same kind of object — name, description,
SOUL, capabilities. `TEMPLATES` below are *pre-fills the user edits*, never a permitted set;
nothing here (or in the schema, or in the UI) may restrict `role` to a list.

**2. Capabilities, not role names, are what the runtime enforces.** A capability the
assistant was not granted is refused by the control plane itself (see
`server.assistant_runtime` and the `/api/assistant/*` routes), so a SOUL that *claims* it may
edit code cannot make it so. `require()` is the single choke point.
"""
import hashlib
import json
import logging
import secrets
from typing import Any, Optional

from server import crypto
from server.db import pool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# capabilities — the enforced vocabulary
# ---------------------------------------------------------------------------
# Each entry: id -> (label, what it lets the assistant actually do, is it safe-by-default)
# `safe_default` False means: the capability EXISTS and is enforced, but a template never
# switches it on for you — unattended writes/deploys need the quota + guard work first
# (v1 scope decision, stated honestly in the UI rather than quietly enabled).
CAPABILITIES: dict[str, dict] = {
    "read_repo": {
        "label": "Read the repo",
        "detail": "Clone the project's git checkout and read/search files with real CLI tools.",
        "safe_default": True,
    },
    "comment": {
        "label": "Comment",
        "detail": "Post findings and proposals into the project thread the owner reads.",
        "safe_default": True,
    },
    "read_costs": {
        "label": "Read usage & cost",
        "detail": "Read the project's token/cost accounting and container metrics.",
        "safe_default": True,
    },
    "run_qa": {
        "label": "Run QA",
        "detail": "Exercise the live app through chrome-pool and file what it finds.",
        "safe_default": True,
    },
    "edit_code": {
        "label": "Edit code",
        "detail": "Write files in its own workspace checkout (never the pipeline's tree).",
        "safe_default": False,
    },
    "commit_push": {
        "label": "Commit & push",
        "detail": "Commit its edits and push them to the project's git repo.",
        "safe_default": False,
    },
    "request_deploy": {
        "label": "Request a deploy",
        "detail": ("Ask the control plane to run the update pipeline. The assistant NEVER "
                   "touches Docker itself — the normalizer, health gate and rollback all "
                   "still apply."),
        "safe_default": False,
    },
}

CAPABILITY_IDS = tuple(CAPABILITIES)


def sanitize_capabilities(items: Any) -> list[str]:
    """Keep only known capability ids, de-duplicated, in the canonical order.

    Unknown strings are DROPPED rather than stored: a capability the runtime does not know
    how to enforce must never sit in the row looking like a grant."""
    if not isinstance(items, (list, tuple, set)):
        return []
    got = {str(c).strip() for c in items}
    return [c for c in CAPABILITY_IDS if c in got]


def has(assistant: dict, capability: str) -> bool:
    caps = assistant.get("capabilities") or []
    if isinstance(caps, str):                       # jsonb may arrive as text
        try:
            caps = json.loads(caps)
        except Exception:  # noqa: BLE001
            caps = []
    return capability in set(caps or ())


class Denied(PermissionError):
    """Raised when an assistant attempts something it was not granted."""


def require(assistant: dict, capability: str) -> None:
    """THE choke point. Every capability-gated path calls this and nothing else."""
    if capability not in CAPABILITIES:
        raise Denied(f"unknown capability {capability!r}")
    if not has(assistant, capability):
        raise Denied(
            f"assistant {assistant.get('id')} ({assistant.get('role') or 'assistant'}) "
            f"was not granted `{capability}`")


# ---------------------------------------------------------------------------
# starter templates — PRE-FILLS, NOT A MENU OF ALLOWED ROLES
# ---------------------------------------------------------------------------
def _soul(who: str, optimises: str, does: list[str], never: list[str], acts_when: str) -> str:
    bullets = "\n".join(f"- {d}" for d in does)
    nevers = "\n".join(f"- {n}" for n in never)
    return (
        f"# Who I am\n{who}\n\n"
        f"# What I optimise for\n{optimises}\n\n"
        f"# What I do on a beat\n{bullets}\n\n"
        f"# What I must never do\n{nevers}\n\n"
        f"# When a beat is worth acting on\n{acts_when}\n"
    )


TEMPLATES: list[dict] = [
    {
        "key": "product_owner",
        "role": "Product Owner",
        "name": "Product Owner",
        "description": "Keeps the product worth using: reads the goals, judges the gap, proposes the next thing.",
        "optimises": "the product is worth using",
        "capabilities": ["read_repo", "comment", "read_costs"],
        "interval_minutes": 120,
        "soul_md": _soul(
            "I am the product owner for this app. I did not write the code and I do not care how "
            "clever it is — I care whether a real person gets what they came for.",
            "The product being genuinely worth using: the shortest path from landing on it to the "
            "value it promised.",
            ["Read the strategy docs and the backlog and compare them with what actually shipped.",
             "Name the single biggest gap between the promise and the product.",
             "Propose the next change as one concrete, buildable request — not a wish list."],
            ["Never rewrite the app's goals to match what was built.",
             "Never propose more than two changes in one beat.",
             "Never touch code."],
            "Act when the gap I can name is bigger than the gap I named last beat. If nothing has "
            "changed and I have nothing new to say, stay quiet and say so.",
        ),
    },
    {
        "key": "developer",
        "role": "Developer",
        "name": "Developer",
        "description": "Keeps it working and keeps it clean. Reads the code, spots rot, proposes (or makes) the fix.",
        "optimises": "it works and stays clean",
        "capabilities": ["read_repo", "comment"],
        "interval_minutes": 60,
        "soul_md": _soul(
            "I am the engineer on this app. The code is mine to keep honest.",
            "Correctness first, then the code staying something a human can still change next month.",
            ["Read the recent commits and the files they touched.",
             "Look for the bug that has not surfaced yet: unhandled errors, unbounded reads, "
             "un-parameterised SQL, a route with no failure path.",
             "Describe the fix precisely enough that someone could apply it in one sitting."],
            ["Never load a whole file into memory to check one line.",
             "Never interpolate a value into SQL.",
             "Never claim something is fixed that I have not verified."],
            "Act on a concrete defect or a concrete piece of rot. 'The code could be nicer' is not "
            "a reason to act.",
        ),
    },
    {
        "key": "tester",
        "role": "Tester",
        "name": "Tester",
        "description": "Uses the live app like a stubborn user and files what actually breaks.",
        "optimises": "it actually works for a user",
        "capabilities": ["read_repo", "run_qa", "comment"],
        "interval_minutes": 60,
        "soul_md": _soul(
            "I am the tester. I do not read the code to decide whether it works — I use the app.",
            "That a real user, on the real deployed URL, can complete the thing the app exists for.",
            ["Exercise the live app end to end and record what happened, not what should have.",
             "File each finding with the exact steps that produced it.",
             "Re-check the findings from my last beat before filing anything new."],
            ["Never file a finding I cannot reproduce.",
             "Never report a pass I did not observe — an HTTP 200 is not a working feature.",
             "Never change code to make a test pass."],
            "Act whenever the app has been deployed since my last beat, or whenever a previous "
            "finding is still open.",
        ),
    },
    {
        "key": "domain_expert",
        "role": "Domain Expert",
        "name": "Domain Expert",
        "description": "Knows the field the app is in and says when the app is wrong about it. Comments only.",
        "optimises": "it is right for the domain",
        "capabilities": ["read_repo", "comment"],
        "interval_minutes": 240,
        "soul_md": _soul(
            "I am the domain expert. I know how this field actually works in practice, including "
            "the parts practitioners never write down.",
            "The app being *right* for its domain — correct vocabulary, correct workflow, correct "
            "assumptions about who is using it and why.",
            ["Read the goals and the visible product and check them against how the work is really done.",
             "Name where the app assumes something that is not true in this field.",
             "Give the correct version, concretely."],
            ["Never write or change code — I advise, I do not implement.",
             "Never invent a domain fact I am not confident about; say I am unsure instead."],
            "Act when I find an assumption that would embarrass the app in front of a practitioner.",
        ),
    },
    {
        "key": "security",
        "role": "Security",
        "name": "Security assistant",
        "description": "Reads code and dependencies looking for how this gets abused. Read-only by design.",
        "optimises": "it can't be abused",
        "capabilities": ["read_repo", "comment"],
        "interval_minutes": 240,
        "soul_md": _soul(
            "I am the security reviewer for this app. I assume every input is hostile and every "
            "user is not who they say they are.",
            "That the app cannot be abused: no injection, no auth bypass, no secret in the repo, "
            "no unbounded resource an anonymous caller can pull on.",
            ["Search the repo for secrets, string-interpolated SQL, missing authorization checks, "
             "and unbounded reads.",
             "Read the dependency manifest and flag anything abandoned or known-bad.",
             "Report each issue with the file, the line and the attack it enables."],
            ["Never write to the repo — I am deliberately read-only.",
             "Never post a secret's value into a finding; report where it is, not what it is.",
             "Never report a theoretical issue as if it were exploitable here."],
            "Act on anything exploitable. Say plainly when a beat found nothing — silence is not "
            "the same as a clean review.",
        ),
    },
    {
        "key": "expense",
        "role": "Expense Management",
        "name": "Expense management assistant",
        "description": "Watches what this project costs to run and says something before the bill does.",
        "optimises": "it doesn't quietly cost money",
        "capabilities": ["read_costs", "comment"],
        "interval_minutes": 720,
        "soul_md": _soul(
            "I watch what this project costs. Nobody else is looking at the bill until it is due.",
            "That the project's cost stays proportionate to its value, and that any change in the "
            "trend is noticed while it is still small.",
            ["Read the token/cost accounting and the container metrics.",
             "Compare this period with the last one and name the delta and its cause.",
             "Flag anything that is growing faster than the project's usage."],
            ["Never guess at a number I can read.",
             "Never raise an alarm about a cost that is flat.",
             "Never suggest a saving that would break the product."],
            "Act when spend moved materially, or when something is trending in a direction that "
            "will matter in a month.",
        ),
    },
]

TEMPLATES_BY_KEY = {t["key"]: t for t in TEMPLATES}


def template_list() -> list[dict]:
    """The starter templates, for the UI's picker. Explicitly labelled as pre-fills."""
    return [
        {"key": t["key"], "role": t["role"], "name": t["name"],
         "description": t["description"], "optimises": t["optimises"],
         "capabilities": list(t["capabilities"]),
         "interval_minutes": t["interval_minutes"], "soul_md": t["soul_md"]}
        for t in TEMPLATES
    ]


def default_soul(role: str, name: str, description: str) -> str:
    """A SOUL for a role we have no template for — because a user may invent any role."""
    role = (role or "Assistant").strip()
    return _soul(
        f"I am the {role} for this app." + (f" {description.strip()}" if description else ""),
        f"Whatever a good {role.lower()} would optimise for on a software project like this one.",
        ["Read the project's current state — goals, recent commits, health, backlog.",
         f"Judge it the way a {role.lower()} would.",
         "Say the single most useful thing I can say this beat, concretely."],
        ["Never act outside the capabilities I was granted.",
         "Never claim work I did not do."],
        "Act when something has changed that a person in my role would want to respond to.",
    )


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------
MIN_INTERVAL_MIN = 5          # a heartbeat faster than this is a cost bug, not a feature
MAX_INTERVAL_MIN = 60 * 24 * 7
MAX_SOUL_CHARS = 20000
MAX_ASSISTANTS_PER_PROJECT = 12


def clamp_interval(v: Any, fallback: int = 60) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return fallback
    return max(MIN_INTERVAL_MIN, min(MAX_INTERVAL_MIN, n))


def slug(role: str) -> str:
    """`docs/assistants/<slug>.SOUL.md` — the on-repo home of a SOUL."""
    out = "".join(c.lower() if c.isalnum() else "-" for c in (role or "assistant"))
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:48] or "assistant"


# ---------------------------------------------------------------------------
# tokens: one credential per assistant, scoped to {project, assistant}
# ---------------------------------------------------------------------------
def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_token() -> tuple[str, str, str]:
    """-> (plaintext, encrypted, sha256). The plaintext is handed to ONE beat container."""
    tok = "asst_" + secrets.token_urlsafe(32)
    return tok, crypto.encrypt(tok), _sha(tok)


# ---------------------------------------------------------------------------
# data access (parameterized SQL only; writes verified — never trust an implicit success)
# ---------------------------------------------------------------------------
_COLS = ("id, project_id, role, name, description, soul_md, capabilities, interval_minutes, "
         "status, last_beat_at, next_beat_at, beat_owner, beat_claimed_at, created_at, "
         "updated_at")


def _row(r) -> dict:
    d = dict(r)
    caps = d.get("capabilities")
    if isinstance(caps, str):
        try:
            caps = json.loads(caps)
        except Exception:  # noqa: BLE001
            caps = []
    d["capabilities"] = caps or []
    return d


async def list_for_project(project_id: str) -> list[dict]:
    rows = await pool().fetch(
        f"SELECT {_COLS} FROM builderapps.assistants WHERE project_id=$1 "
        "ORDER BY created_at LIMIT 100", project_id)
    return [_row(r) for r in rows]


async def get(assistant_id: int, project_id: Optional[str] = None) -> Optional[dict]:
    """Fetch one assistant. Passing `project_id` makes it project-scoped, which is how every
    user-facing route reads it — another project's assistant is simply not found."""
    if project_id is not None:
        r = await pool().fetchrow(
            f"SELECT {_COLS} FROM builderapps.assistants WHERE id=$1 AND project_id=$2",
            assistant_id, project_id)
    else:
        r = await pool().fetchrow(
            f"SELECT {_COLS} FROM builderapps.assistants WHERE id=$1", assistant_id)
    return _row(r) if r else None


async def get_by_token(token: str) -> Optional[dict]:
    """Resolve a beat container's credential. Looked up by hash — the plaintext is never
    compared against a decrypted column, and an empty token can never match."""
    if not token or not token.startswith("asst_"):
        return None
    r = await pool().fetchrow(
        f"SELECT {_COLS} FROM builderapps.assistants WHERE token_sha=$1", _sha(token))
    return _row(r) if r else None


async def token_for(assistant_id: int) -> str:
    """The plaintext control-plane token, decrypted for one container launch."""
    enc = await pool().fetchval(
        "SELECT token_enc FROM builderapps.assistants WHERE id=$1", assistant_id)
    return crypto.decrypt(enc) if enc else ""


async def count_for_project(project_id: str) -> int:
    return int(await pool().fetchval(
        "SELECT count(*) FROM builderapps.assistants WHERE project_id=$1", project_id) or 0)


async def create(*, project_id: str, role: str, name: str, description: str,
                 soul_md: str, capabilities: list[str], interval_minutes: int,
                 status: str = "paused") -> dict:
    _plain, enc, sha = mint_token()
    row = await pool().fetchrow(
        "INSERT INTO builderapps.assistants "
        "(project_id, role, name, description, soul_md, capabilities, interval_minutes, "
        " status, token_enc, token_sha, next_beat_at) "
        "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10, "
        "        CASE WHEN $8='active' THEN now() ELSE NULL END) "
        f"RETURNING {_COLS}",
        project_id, role[:120], name[:120], description[:2000], soul_md[:MAX_SOUL_CHARS],
        json.dumps(capabilities), interval_minutes, status, enc, sha,
    )
    if not row:
        raise RuntimeError("create assistant returned no row")
    return _row(row)


async def update(assistant_id: int, project_id: str, **fields) -> Optional[dict]:
    """PATCH the editable columns. Unknown/None fields are ignored (a PATCH is partial)."""
    sets, args = [], []
    allowed = {"role": 120, "name": 120, "description": 2000, "soul_md": MAX_SOUL_CHARS}
    for col, cap in allowed.items():
        if fields.get(col) is not None:
            args.append(str(fields[col])[:cap])
            sets.append(f"{col}=${len(args) + 2}")
    if fields.get("interval_minutes") is not None:
        args.append(clamp_interval(fields["interval_minutes"]))
        sets.append(f"interval_minutes=${len(args) + 2}")
    if fields.get("capabilities") is not None:
        args.append(json.dumps(sanitize_capabilities(fields["capabilities"])))
        sets.append(f"capabilities=${len(args) + 2}::jsonb")
    if not sets:
        return await get(assistant_id, project_id)
    row = await pool().fetchrow(
        "UPDATE builderapps.assistants SET " + ", ".join(sets) + ", updated_at=now() "
        f"WHERE id=$1 AND project_id=$2 RETURNING {_COLS}",
        assistant_id, project_id, *args)
    return _row(row) if row else None


async def set_status(assistant_id: int, project_id: str, status: str) -> Optional[dict]:
    """active | paused. Going active schedules the first beat immediately, which is what a
    user pressing Start expects — otherwise the assistant looks dead for an hour."""
    row = await pool().fetchrow(
        "UPDATE builderapps.assistants SET status=$3, updated_at=now(), "
        "  next_beat_at = CASE WHEN $3='active' THEN COALESCE(next_beat_at, now()) "
        "                      ELSE next_beat_at END "
        f"WHERE id=$1 AND project_id=$2 RETURNING {_COLS}",
        assistant_id, project_id, status)
    return _row(row) if row else None


async def delete(assistant_id: int, project_id: str) -> bool:
    res = await pool().execute(
        "DELETE FROM builderapps.assistants WHERE id=$1 AND project_id=$2",
        assistant_id, project_id)
    return res.endswith(" 1")


async def due_now(limit: int = 20) -> list[dict]:
    """Active assistants whose next_beat_at has passed and that nobody is beating."""
    rows = await pool().fetch(
        f"SELECT {_COLS} FROM builderapps.assistants "
        "WHERE status='active' AND next_beat_at IS NOT NULL AND next_beat_at <= now() "
        "  AND (beat_owner = '' OR beat_owner IS NULL) "
        "ORDER BY next_beat_at LIMIT $1", max(1, min(int(limit), 100)))
    return [_row(r) for r in rows]


async def claim(assistant_id: int, owner: str) -> Optional[dict]:
    """Atomically take a due assistant. This single UPDATE is the cross-process mutex — two
    schedulers racing on the same assistant, exactly one wins. Same shape as `claim_run`."""
    row = await pool().fetchrow(
        "UPDATE builderapps.assistants SET beat_owner=$2, beat_claimed_at=now() "
        "WHERE id=$1 AND status='active' AND (beat_owner='' OR beat_owner IS NULL) "
        f"RETURNING {_COLS}", assistant_id, owner)
    return _row(row) if row else None


async def release(assistant_id: int, *, schedule_next_minutes: Optional[int] = None) -> None:
    """Drop the claim and (optionally) set the next beat. Always called in a `finally`, so a
    crashed beat can never leave an assistant permanently claimed."""
    if schedule_next_minutes is None:
        await pool().execute(
            "UPDATE builderapps.assistants SET beat_owner='', beat_claimed_at=NULL, "
            "updated_at=now() WHERE id=$1", assistant_id)
    else:
        await pool().execute(
            "UPDATE builderapps.assistants SET beat_owner='', beat_claimed_at=NULL, "
            "last_beat_at=now(), "
            "next_beat_at = now() + make_interval(mins => $2::int), updated_at=now() "
            "WHERE id=$1", assistant_id, int(schedule_next_minutes))


async def sweep_orphaned_claims(owner_not: str) -> int:
    """BOOT SWEEP. A control-plane redeploy kills any beat container's supervisor; the row it
    left claimed would otherwise never beat again (the exact bug class that once stranded
    every build). Anything claimed by a process that is not us is by definition dead."""
    res = await pool().execute(
        "UPDATE builderapps.assistants SET beat_owner='', beat_claimed_at=NULL "
        "WHERE beat_owner <> '' AND beat_owner <> $1", owner_not)
    try:
        return int(str(res).rsplit(" ", 1)[-1])
    except Exception:  # noqa: BLE001
        return 0


async def force_due(assistant_id: int, project_id: str) -> bool:
    """`POST /beat` — make it due right now regardless of schedule or status."""
    res = await pool().execute(
        "UPDATE builderapps.assistants SET next_beat_at=now(), updated_at=now() "
        "WHERE id=$1 AND project_id=$2", assistant_id, project_id)
    return res == "UPDATE 1"


# ---- beats ----------------------------------------------------------------
_BEAT_COLS = ("id, assistant_id, project_id, status, trigger_kind, thought, actions, log, "
              "tokens, cost_usd, duration_ms, ts, finished_at")


def _beat_row(r) -> dict:
    d = dict(r)
    acts = d.get("actions")
    if isinstance(acts, str):
        try:
            acts = json.loads(acts)
        except Exception:  # noqa: BLE001
            acts = []
    d["actions"] = acts or []
    d["cost_usd"] = float(d.get("cost_usd") or 0)
    return d


async def start_beat(assistant_id: int, project_id: str, trigger_kind: str) -> int:
    """Open the beat row BEFORE the container starts, so the UI shows a running beat
    immediately and a beat that dies mid-flight still leaves a trace."""
    bid = await pool().fetchval(
        "INSERT INTO builderapps.assistant_beats"
        "(assistant_id, project_id, status, trigger_kind) VALUES ($1,$2,'running',$3) "
        "RETURNING id", assistant_id, project_id, trigger_kind[:16])
    if bid is None:
        raise RuntimeError("start_beat returned no id")
    return int(bid)


async def finish_beat(beat_id: int, *, status: str, thought: str = "",
                      actions: Optional[list] = None, log: str = "", tokens: int = 0,
                      cost_usd: float = 0.0, duration_ms: int = 0) -> None:
    res = await pool().execute(
        "UPDATE builderapps.assistant_beats SET status=$2, thought=$3, actions=$4::jsonb, "
        "log=$5, tokens=$6, cost_usd=$7, duration_ms=$8, finished_at=now() WHERE id=$1",
        beat_id, status[:16], (thought or "")[:8000], json.dumps(actions or []),
        (log or "")[:20000], int(tokens or 0), float(cost_usd or 0.0), int(duration_ms or 0))
    if res != "UPDATE 1":
        raise RuntimeError(f"finish_beat affected {res!r} for {beat_id}")


async def list_beats(assistant_id: int, limit: int = 40) -> list[dict]:
    rows = await pool().fetch(
        f"SELECT {_BEAT_COLS} FROM builderapps.assistant_beats WHERE assistant_id=$1 "
        "ORDER BY ts DESC LIMIT $2", assistant_id, max(1, min(int(limit), 200)))
    return [_beat_row(r) for r in rows]


async def last_beat(assistant_id: int) -> Optional[dict]:
    r = await pool().fetchrow(
        f"SELECT {_BEAT_COLS} FROM builderapps.assistant_beats WHERE assistant_id=$1 "
        "AND status <> 'running' ORDER BY ts DESC LIMIT 1", assistant_id)
    return _beat_row(r) if r else None


async def fail_running_beats(assistant_id: int, reason: str) -> int:
    """Any beat still `running` for this assistant is a corpse (the container is gone).
    Recorded as failed with a reason rather than left spinning forever."""
    res = await pool().execute(
        "UPDATE builderapps.assistant_beats SET status='failed', "
        "log = left(coalesce(log,'') || $2, 20000), finished_at=now() "
        "WHERE assistant_id=$1 AND status='running'",
        assistant_id, f"\n[interrupted] {reason}"[:2000])
    try:
        return int(str(res).rsplit(" ", 1)[-1])
    except Exception:  # noqa: BLE001
        return 0


async def sweep_running_beats(reason: str) -> int:
    """Boot-time equivalent across every assistant."""
    res = await pool().execute(
        "UPDATE builderapps.assistant_beats SET status='failed', "
        "log = left(coalesce(log,'') || $1, 20000), finished_at=now() WHERE status='running'",
        f"\n[interrupted] {reason}"[:2000])
    try:
        return int(str(res).rsplit(" ", 1)[-1])
    except Exception:  # noqa: BLE001
        return 0
