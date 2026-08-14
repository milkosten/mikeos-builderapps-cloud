"""Offline tests for phase 30 — the assistant that ships code.

Three properties are worth a test because getting them wrong is expensive and silent:

1. **The bounds on an unattended write actually bound it.** The gate that inspects what the
   coding agent produced is the only thing between "an agent edits a feature" and "an agent
   edits the migration runner". It runs on real git state in a temp repo here, not on a mock.

2. **The LLM proxy pins the model.** The container names a model; the proxy must ignore it.
   If that ever silently passes through, a compromised or confused container picks the most
   expensive model on OpenRouter and nobody notices until the bill.

3. **The activity feed cannot be used to smuggle anything.** It is agent-produced text going
   straight to a human's screen, so it is untrusted input like any other.

    python3 -m tests.test_phase30
"""
import importlib.util
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, why=""):
    (PASS if cond else FAIL).append((name, why))
    print(("  ok   " if cond else "  FAIL ") + name + (f": {why}" if not cond and why else ""))


def _load_beat():
    """Import the beat program by path — it lives in the runtime image's build context, not
    in `server/`, and it must be testable without building a container."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assistant-runtime", "beat.py")
    spec = importlib.util.spec_from_file_location("beat_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def main() -> int:
    beat = _load_beat()

    # ---- 1. the change gate, against a REAL git checkout ------------------
    print("\n[1] the gate on what a coding agent produced")
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        for rel, body in (("server.js", "const a = 1;\n"),
                          ("db/migrate.js", "// platform\n"),
                          ("docker-compose.yml", "services: {}\n"),
                          ("docs/assistants/developer.SOUL.md", "# soul\n")):
            os.makedirs(os.path.join(repo, os.path.dirname(rel)) or repo, exist_ok=True)
            with open(os.path.join(repo, rel), "w") as f:
                f.write(body)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "init")
        beat.REPO = repo

        check("an untouched tree is refused as empty",
              beat.gate_changes().get("empty") is True,
              "a beat that changed nothing must not produce a commit")

        with open(os.path.join(repo, "server.js"), "w") as f:
            f.write("const a = 1;\nconst b = 2;\n")
        g = beat.gate_changes()
        check("an ordinary source edit passes", g["ok"] and g["files"] == ["server.js"])
        git(repo, "reset", "-q")
        git(repo, "checkout", "--", ".")

        # Each protected file, one at a time — a loop, so adding one to PROTECTED without
        # adding a case here still gets covered.
        for rel in ("db/migrate.js", "docker-compose.yml",
                    "docs/assistants/developer.SOUL.md"):
            with open(os.path.join(repo, rel), "a") as f:
                f.write("\n// agent was here\n")
            g = beat.gate_changes()
            check(f"editing {rel} is refused", not g["ok"] and "protected" in g["detail"],
                  "the platform's own files are not the agent's to change")
            git(repo, "reset", "-q")
            git(repo, "checkout", "--", ".")

        # A broken .js must never reach a deploy — the same rule the build pipeline applies.
        with open(os.path.join(repo, "server.js"), "w") as f:
            f.write("const a = ;\n")
        g = beat.gate_changes()
        node = subprocess.run(["node", "--version"], capture_output=True)
        if node.returncode == 0:
            check("syntactically broken javascript is refused",
                  not g["ok"] and "does not parse" in g["detail"])
        else:
            check("syntax gate skipped (no node on this box)", True)
        git(repo, "reset", "-q")
        git(repo, "checkout", "--", ".")

        # REGRESSION — this one cost a real, complete beat. `git status --porcelain`
        # COLLAPSES an untracked directory into a single entry, so a brand-new
        # `docs/assistants/` was reported as the directory itself, matched the
        # protected-prefix rule, and the gate discarded an entire working change. Two things
        # must hold: files are listed individually (`-uall`), and the SOUL mirror that the
        # BEAT PROGRAM itself wrote is not mistaken for the agent reaching into a protected
        # path.
        # It has to be a repo where `docs/assistants/` does NOT yet exist in HEAD — that is
        # the whole point: the directory is brand new, so git collapses it.
        with tempfile.TemporaryDirectory() as tmp2:
            fresh = os.path.join(tmp2, "repo")
            os.makedirs(fresh)
            git(fresh, "init", "-q")
            git(fresh, "config", "user.email", "t@t")
            git(fresh, "config", "user.name", "t")
            os.makedirs(os.path.join(fresh, "docs"))
            for rel, body in (("server.js", "const a = 1;\n"), ("docs/VISION.md", "v\n")):
                with open(os.path.join(fresh, rel), "w") as f:
                    f.write(body)
            git(fresh, "add", "-A")
            git(fresh, "commit", "-qm", "init")
            beat.REPO = fresh
            beat.SOUL_MIRRORED.clear()
            os.makedirs(os.path.join(fresh, "docs", "assistants"))
            with open(os.path.join(fresh, "docs/assistants/developer.SOUL.md"), "w") as f:
                f.write("# soul\n")
            beat.SOUL_MIRRORED.append("docs/assistants/developer.SOUL.md")
            with open(os.path.join(fresh, "server.js"), "w") as f:
                f.write("const a = 1;\nconst feature = true;\n")
            g = beat.gate_changes()
            check("a NEW untracked directory is listed as its FILES, not as the directory",
                  "docs/assistants/developer.SOUL.md" in g.get("files", []),
                  f"got {g.get('files')} — plain --porcelain would say 'docs/assistants/'")
            check("the beat's own SOUL mirror does not trip the protected-path rule",
                  g.get("ok"), g.get("detail", ""))
            with open(os.path.join(fresh, "docs/assistants/sneaky.md"), "w") as f:
                f.write("x\n")
            g = beat.gate_changes()
            check("but any OTHER file under docs/assistants/ is still refused",
                  not g.get("ok") and "protected" in g.get("detail", ""),
                  "the agent still cannot edit its own soul")
            beat.SOUL_MIRRORED.clear()
        beat.REPO = repo

        # The agent was TOLD not to commit, but it has a bash tool and "told not to" is a
        # request. If it commits anyway, `git status` is clean and a naive gate would say
        # "changed nothing" and throw the work away. It must be folded back instead.
        git(repo, "branch", "-f", "origin/HEAD")          # stand in for the remote ref
        with open(os.path.join(repo, "server.js"), "w") as f:
            f.write("const a = 1;\nconst agent = true;\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "the agent committed by itself")
        check("a self-committing agent's work is not lost",
              beat.gate_changes().get("ok") and
              beat.gate_changes().get("files") == ["server.js"],
              "reset --soft puts it back in the working tree so the ONE gate sees it")
        git(repo, "reset", "-q", "--hard", "origin/HEAD")

        # The file-count cap: a "change" that rewrites the world is a rewrite, not a change.
        beat.MAX_CHANGED_FILES = 3
        for i in range(5):
            with open(os.path.join(repo, f"extra{i}.txt"), "w") as f:
                f.write("x\n")
        g = beat.gate_changes()
        check("too many files in one beat is refused",
              not g["ok"] and "files changed in one beat" in g["detail"])

    # ---- 2. what the docs loader promises --------------------------------
    print("\n[2] the docs are loaded deterministically, and truncation is admitted")
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, "docs"))
        with open(os.path.join(repo, "docs", "VISION.md"), "w") as f:
            f.write("V" * (beat.DOC_CHARS_EACH * 3))
        beat.REPO = repo
        block, note = beat.load_docs()
        check("an oversized document is truncated, not dropped",
              block.startswith("## docs/VISION.md") and "truncated" in note,
              "silently dropping the vision is how an assistant optimises the wrong thing")
        check("the per-document cap holds", len(block) <= beat.DOC_CHARS_EACH + 200)
        check("a missing document is reported, not hidden", "missing" in note)

    # ---- 3. the LLM proxy pins the model ---------------------------------
    print("\n[3] the LLM proxy")
    os.environ.setdefault("SECRETS_KEY", "x" * 32)
    from server import llm_proxy
    check("the proxy pins one model", bool(llm_proxy.PINNED_MODEL),
          "a container that could choose its own model could choose an expensive one")
    check("a per-beat spend cap exists and is finite",
          0 < llm_proxy.BEAT_COST_CAP < 100,
          "Pi has no built-in step or cost cap; this is the only backstop")
    llm_proxy._beat_cost.clear()
    llm_proxy._charge(7, 0.4)
    llm_proxy._charge(7, 0.35)
    check("proxy spend accumulates per beat", abs(llm_proxy.beat_cost(7) - 0.75) < 1e-9)
    check("reading the beat's spend clears it", llm_proxy.forget_beat(7) == 0.75
          and llm_proxy.beat_cost(7) == 0.0,
          "the control plane adds it to the beat record exactly once")
    check("an unknown beat costs nothing", llm_proxy.beat_cost(None) == 0.0)

    # ---- 4. the activity feed is untrusted input --------------------------
    print("\n[4] the activity feed")
    from server import assistants as A
    out = A.sanitize_activity([
        {"kind": "tool", "icon": "✎", "text": "editing server.js"},
        {"kind": "not-a-kind", "text": "x" * 5000, "detail": "y" * 5000},
        {"text": "   "},                       # empty after strip -> dropped
        "not a dict",
        {"kind": "result", "text": "ok", "ok": "yes"},
    ])
    check("empty and malformed lines are dropped", len(out) == 3)
    check("an unknown kind falls back to a known one",
          all(o["kind"] in ("phase", "tool", "text", "result") for o in out))
    check("text is clamped", all(len(o["text"]) <= 400 for o in out))
    check("detail is clamped", all(len(o.get("detail", "")) <= 600 for o in out))
    check("ok is coerced to a bool", isinstance(out[2]["ok"], bool))
    check("a huge batch is bounded", len(A.sanitize_activity(
        [{"text": "line"}] * 1000)) <= 200)

    # ---- 5. the ship pipeline is not the build pipeline -------------------
    print("\n[5] assistants are disconnected from the build pipeline")
    import server.shipper as shipper
    src = open(shipper.__file__).read()
    check("the shipper never calls the build pipeline",
          "run_update" not in src and "run_create" not in src,
          "request_deploy must not re-plan a diff with a second LLM and fight the agent")
    check("the shipper has exactly the three infrastructure steps",
          shipper.STEP_NAMES == ("ship_checkout", "ship_deploy", "ship_record"))
    from server.harness import engine
    check("every ship step is fatal, none skippable",
          all(not engine.is_skippable(s) for s in shipper.STEP_NAMES),
          "you cannot skip the health gate and still have shipped anything")
    check("the ship steps have their own timeouts",
          all(engine.step_timeout(s) > 0 for s in shipper.STEP_NAMES)
          and engine.step_timeout("ship_deploy") >= 3600,
          "ship_deploy may run the build AND the rollback build")

    from server import assistant_runtime as R
    rsrc = open(R.__file__).read()
    check("act_request_deploy routes to the shipper, not the pipeline",
          "shipper.run_ship(" in rsrc and "pipeline.run_update(" not in rsrc,
          "a CALL, not a mention — the docstring explains why it is not the update pipeline")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name, why in FAIL:
        print(f"  FAIL {name}" + (f": {why}" if why else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
