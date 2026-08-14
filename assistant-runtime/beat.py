#!/usr/bin/env python3
"""ONE beat of ONE assistant. The container is the beat; when this exits, the beat is over.

    perceive  -> refresh the checkout, LOAD THE PROJECT'S DOCS (deterministically — see
                 below), look at the tree with real tools, ask the control plane for state
    reason    -> ONE LLM call, made BY the control plane (so no model key ever lives in this
                 container): docs + SOUL + role + context -> {thought, actions[], done}
    act       -> if the decision is to code, hand the task to **Pi**, the open-source coding
                 agent installed in this image, running on the checkout. Then gate the diff,
                 commit, push, and ask the control plane to ship HEAD.
    remember  -> POST the beat record (thought + actions + tokens + cost) back

## We do not write our own coding loop

The read/grep/edit loop belongs to Pi (`@earendil-works/pi-coding-agent`, pinned in the
Dockerfile). This program's job around it is the part Pi should not own: what the task IS,
what the agent is allowed to touch, and what happens to the result.

## The docs are loaded BY DESIGN, not by hope

Mike's rule: when the container starts, *we* put the project's own documents in front of the
agent — `docs/VISION.md` (vision, mission, objectives), `ICP.md`, `UX.md`,
`BUYER-PERSONA.md`, `MARKETING.md`, `TECHNICAL-PLAN.md` — before it reasons about anything.
An assistant with judgment but no knowledge of what the product is *for* optimises the wrong
thing. It is never left to the model to decide to go and read them, and the grounding order
is fixed: **docs -> SOUL/role/capabilities -> repo state + recent beats -> the task.**

## What keeps this safe

* No model credential here: Pi is pointed at the control plane's OpenAI-compatible proxy and
  authenticates with the per-assistant token this container already holds.
* No Docker socket, ever. Deploying is asking.
* Pi has no built-in step or cost cap (verified against 0.84.2), so there are two external
  ones: a hard wall-clock timeout here, and a per-beat spend cap at the proxy.
* Pi edits the working tree; it never commits (verified — it runs no git write commands).
  Everything it produced is inspected by `gate_changes()` BEFORE a commit happens.

Stdlib only, on purpose: the beat program must not need a package install to start.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

CONTROL_URL = os.environ.get("CONTROL_URL", "").rstrip("/")
TOKEN = os.environ.get("ASSISTANT_TOKEN", "")
BEAT_ID = os.environ.get("BEAT_ID", "")
ASSISTANT_ID = os.environ.get("ASSISTANT_ID", "")
PROJECT_ID = os.environ.get("PROJECT_ID", "")
TRIGGER_KIND = os.environ.get("TRIGGER_KIND", "schedule")
GIT_REMOTE = os.environ.get("GIT_REMOTE", "")
CAPS = {c.strip() for c in os.environ.get("CAPABILITIES", "").split(",") if c.strip()}

WORKSPACE = "/workspace"
REPO = os.path.join(WORKSPACE, "repo")
PI_DIR = os.environ.get("PI_CODING_AGENT_DIR", "/tmp/pi/agent")
PI_MODEL = os.environ.get("PI_MODEL", "builderapps/kimi-k3")
PI_TIMEOUT_SEC = int(os.environ.get("PI_TIMEOUT_SEC", "780"))
DEPLOY_WAIT_SEC = int(os.environ.get("DEPLOY_WAIT_SEC", "900"))

# House rule: never slurp a file into RAM without a cap.
FILE_CAP = 200 * 1024
LOG: list[str] = []

# ---------------------------------------------------------------------------
# the grounding documents, in the order they are handed over
# ---------------------------------------------------------------------------
DOC_FILES = ("docs/VISION.md", "docs/TECHNICAL-PLAN.md", "docs/UX.md", "docs/ICP.md",
             "docs/BUYER-PERSONA.md", "docs/MARKETING.md")
DOC_CHARS_EACH = 7000
DOC_CHARS_TOTAL = 26000

# ---------------------------------------------------------------------------
# what an assistant may NOT change, and how much it may change at once
# ---------------------------------------------------------------------------
# These are the files that make the platform work rather than the product. An agent editing
# them is not building a feature — it is reaching outside its box, and the answer is no. The
# check runs on what Pi ACTUALLY touched, after the fact, because Pi is free-form by design;
# a violation reverts the whole beat's work rather than committing part of it.
PROTECTED = (
    "db/migrate.js",              # the platform's migration runner
    "docker-compose.yml",         # infrastructure — the normalizer owns this
    "docker-compose.normalized.yml",
    "Dockerfile",
    ".dockerignore",
    ".env",
    "builderapps.json",
    "current_work.json",
)
PROTECTED_PREFIXES = (".git/", "docs/assistants/")   # incl. its own SOUL: not self-editable
MAX_CHANGED_FILES = int(os.environ.get("MAX_CHANGED_FILES", "12"))
MAX_DIFF_BYTES = int(os.environ.get("MAX_DIFF_BYTES", "220000"))


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG.append(line)
    print(line, flush=True)


def redact(s: str) -> str:
    """A token must never reach a log line or a beat record."""
    s = re.sub(r"asst_[A-Za-z0-9_\-]{8,}", "asst_***", s or "")
    return re.sub(r"://[^/@\s]+:[^/@\s]+@", "://***:***@", s)


# ---------------------------------------------------------------------------
# the activity feed — what the user watches on /builder while this beat runs
# ---------------------------------------------------------------------------
# EVERY line here corresponds to something that actually happened: a tool the coding agent
# really invoked, a file it really wrote, a commit that really landed, a health gate that
# really returned a verdict. Nothing is a plausible-looking progress message invented to
# make the UI look busy. A feed that narrates work it did not observe is worse than a quiet
# one — we have already been burned by verification that lied.
_ACT_BUF: list[dict] = []
_ACT_LAST_FLUSH = [0.0]
ACT_FLUSH_SEC = float(os.environ.get("ACTIVITY_FLUSH_SEC", "1.5"))


def activity(kind: str, icon: str, text: str, detail: str = "", ok=None,
             flush: bool = False) -> None:
    """Queue one activity line; batched so a token-by-token stream is not one POST per word."""
    entry = {"kind": kind, "icon": icon, "text": redact(text)[:400],
             "ts": time.strftime("%H:%M:%S")}
    if detail:
        entry["detail"] = redact(detail)[:600]
    if ok is not None:
        entry["ok"] = bool(ok)
    _ACT_BUF.append(entry)
    if flush or len(_ACT_BUF) >= 12 or (time.monotonic() - _ACT_LAST_FLUSH[0]) >= ACT_FLUSH_SEC:
        flush_activity()


def flush_activity() -> None:
    """Push the queued lines to the control plane. Never raises: the feed is for humans, and
    losing a line must not cost the beat its work."""
    if not _ACT_BUF:
        return
    batch, _ACT_BUF[:] = list(_ACT_BUF), []
    _ACT_LAST_FLUSH[0] = time.monotonic()
    try:
        call("POST", "/api/assistant/activity", {"lines": batch}, timeout=30)
    except Exception as e:
        print("[activity] dropped %d line(s): %s" % (len(batch), redact(str(e))[:200]),
              flush=True)


# ---------------------------------------------------------------------------
# control-plane client
# ---------------------------------------------------------------------------
def call(method: str, path: str, body=None, timeout: float = 240.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        CONTROL_URL + path, data=data, method=method,
        headers={"X-Assistant-Token": TOKEN, "X-Beat-Id": BEAT_ID,
                 "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {redact(detail)}") from None
    except Exception as e:
        raise RuntimeError(f"{method} {path} failed: {redact(str(e))}") from None
    return json.loads(raw) if raw.strip() else {}


def granted(capability: str) -> tuple[bool, str]:
    """Ask the CONTROL PLANE, not this container's own env, whether a capability is held.

    The `CAPABILITIES` env var is a fail-fast convenience; the server-side `require()` is the
    boundary. Checking with the server before doing local work means a revoked capability
    takes effect on the very next beat instead of whenever the container image next changes.
    """
    if capability not in CAPS:
        return False, f"{capability} not granted"
    try:
        res = call("POST", "/api/assistant/act",
                   {"action": {"type": "check", "capability": capability}}, timeout=60)
    except Exception as e:
        return False, f"could not verify {capability}: {redact(str(e))[:200]}"
    if not res.get("ok"):
        return False, str(res.get("detail") or f"{capability} refused")[:200]
    return True, ""


# ---------------------------------------------------------------------------
# shell helper — real tools, bounded
# ---------------------------------------------------------------------------
def sh(args: list[str], cwd: str | None = None, timeout: int = 90,
       env: dict | None = None) -> tuple[int, str]:
    # `cwd=REPO` as a DEFAULT ARGUMENT would bind the module constant once at def time, so
    # anything that repoints REPO (a test, or a future multi-checkout beat) would silently
    # keep shelling out in the old directory. Resolve it per call.
    cwd = cwd or REPO
    try:
        p = subprocess.run(args, cwd=cwd if os.path.isdir(cwd) else None,
                           capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **(env or {})})
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))[:60000]
    except subprocess.TimeoutExpired as e:
        partial = ""
        for chunk in (e.stdout, e.stderr):
            if chunk:
                partial += chunk.decode() if isinstance(chunk, bytes) else chunk
        return 124, f"timed out after {timeout}s: {' '.join(args[:3])}\n{partial[-4000:]}"
    except FileNotFoundError:
        return 127, f"not installed: {args[0]}"


def read_capped(path: str, cap: int = FILE_CAP) -> str:
    """Never load a whole file into RAM (the 1.55 GB video lesson, scaled down)."""
    try:
        if os.path.getsize(path) > cap:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(cap) + "\n… [truncated]"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 1. PERCEIVE — refresh the checkout, then read the docs, then look around
# ---------------------------------------------------------------------------
def refresh_checkout() -> str:
    """Clone on first beat, fetch + hard reset after that (the workspace is a cache, never a
    source of truth). Returns a human line about what happened."""
    if "read_repo" not in CAPS:
        return "read_repo not granted — no checkout for this assistant."
    if not GIT_REMOTE:
        return "no git remote was provided — the control plane could not mint one."
    if os.path.isdir(os.path.join(REPO, ".git")):
        sh(["git", "remote", "set-url", "origin", GIT_REMOTE])
        rc, out = sh(["git", "fetch", "--depth", "50", "origin"], timeout=180)
        if rc != 0:
            return "git fetch failed: " + redact(out[-300:])
        sh(["git", "reset", "--hard", "origin/HEAD"])
        sh(["git", "clean", "-fd"])
        return "refreshed the existing checkout"
    os.makedirs(WORKSPACE, exist_ok=True)
    if os.path.exists(REPO):
        shutil.rmtree(REPO, ignore_errors=True)
    rc, out = sh(["git", "clone", "--depth", "50", GIT_REMOTE, REPO], cwd=WORKSPACE,
                 timeout=300)
    if rc != 0:
        return "git clone failed: " + redact(out[-300:])
    return "cloned the repo for the first time"


def load_docs() -> tuple[str, str]:
    """Read the project's strategy documents into one grounding block. Deterministic.

    Returns (block, note). Size-capped per document and in total: a large doc set must not
    be able to blow the context out. When something is cut, it is TRUNCATED and said so —
    never silently dropped, because "the vision was too long so the agent never saw it" is
    exactly the kind of invisible failure that makes an assistant optimise the wrong thing.
    """
    if not os.path.isdir(REPO):
        return "", "no checkout — the project's documents could not be read this beat"
    parts, notes, budget = [], [], DOC_CHARS_TOTAL
    for rel in DOC_FILES:
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            notes.append(f"{rel}: missing")
            continue
        body = read_capped(path, DOC_CHARS_EACH).strip()
        if not body:
            notes.append(f"{rel}: empty")
            continue
        if len(body) > budget:
            body = body[:max(budget, 0)].rstrip()
            notes.append(f"{rel}: truncated to fit the context budget")
            if not body:
                notes.append(f"{rel}: omitted entirely — the budget was already spent")
                continue
        elif os.path.getsize(path) > DOC_CHARS_EACH:
            notes.append(f"{rel}: truncated at {DOC_CHARS_EACH} chars")
        budget -= len(body)
        parts.append(f"## {rel}\n\n{body}")
        if budget <= 0:
            notes.append("context budget exhausted; later documents were not read")
            break
    block = "\n\n".join(parts)
    note = ("read " + ", ".join(p.split("\n", 1)[0][3:] for p in parts)
            if parts else "no documents found")
    if notes:
        note += " (" + "; ".join(notes) + ")"
    return block, note


def survey_workspace() -> str:
    """What the checkout looks like right now, gathered with the real tools this image ships.

    Note the division of labour: this is for the REASONING call, which runs on the control
    plane and has to decide what is worth doing from a compact summary. Pi does not need it —
    Pi is standing in the repo and reads whatever it likes."""
    if not os.path.isdir(os.path.join(REPO, ".git")):
        return ""
    parts: list[str] = []

    rc, out = sh(["git", "log", "-12", "--pretty=format:%h %ad %an: %s", "--date=short"])
    if rc == 0 and out.strip():
        parts.append("## Recent commits\n" + out.strip()[:2500])

    rc, out = sh(["git", "ls-files"])
    if rc == 0:
        files = [f for f in out.splitlines() if f.strip()]
        parts.append(f"## Tracked files ({len(files)})\n" + "\n".join(sorted(files)[:200]))

    body = read_capped(os.path.join(REPO, "package.json"))
    if body:
        parts.append("## package.json\n" + body[:1500])

    # The main server file's route surface — the single most useful thing for deciding what
    # the app can and cannot currently do.
    rc, out = sh(["rg", "-n", r"app\.(get|post|put|patch|delete|use)\(", "server.js"])
    if rc == 0 and out.strip():
        parts.append("## Routes in server.js\n" + out.strip()[:2500])

    rc, out = sh(["git", "ls-files", "migrations"])
    if rc == 0 and out.strip():
        parts.append("## Migrations\n" + out.strip()[:800])

    rc, out = sh(["rg", "-n", "--max-count", "3", "-e", "TODO", "-e", "FIXME", "-e", "XXX",
                  "--glob", "!node_modules", "."])
    if rc == 0 and out.strip():
        parts.append("## TODO / FIXME markers\n" + out.strip()[:1200])

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 2. THE CODING AGENT — Pi, driven non-interactively
# ---------------------------------------------------------------------------
def write_pi_config() -> None:
    """Point Pi at the control plane's OpenAI-compatible proxy.

    `apiKey: "$ASSISTANT_TOKEN"` and the `X-Beat-Id` header are resolved by Pi from the
    environment at request time, so this file holds no credential either — and the token it
    names is the same scoped, per-assistant one this container was handed for one beat. The
    real provider key stays on the control plane, which is the entire point.

    `supportsDeveloperRole`/`supportsReasoningEffort` are off because the proxy speaks plain
    OpenAI chat-completions (Pi's own docs call for exactly this on proxy-style endpoints).
    """
    os.makedirs(PI_DIR, exist_ok=True)
    cfg = {
        "providers": {
            "builderapps": {
                "baseUrl": f"{CONTROL_URL}/api/assistant/llm/v1",
                "api": "openai-completions",
                "apiKey": "$ASSISTANT_TOKEN",
                "headers": {"X-Beat-Id": "$BEAT_ID"},
                "compat": {"supportsDeveloperRole": False,
                           "supportsReasoningEffort": False},
                "models": [{
                    "id": "kimi-k3",
                    "name": "Kimi k3 (via the builderapps control plane)",
                    "contextWindow": 200000,
                    "maxTokens": 32000,
                    "input": ["text"],
                }],
            }
        }
    }
    with open(os.path.join(PI_DIR, "models.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def build_grounding(docs: str, context: dict, survey: str) -> str:
    """The system-prompt preamble Pi gets, in Mike's order.

    docs (what the product is for) -> SOUL/role/capabilities (who is deciding) -> repo state
    and recent beats (what has already happened) . The task itself is passed separately, as
    the user message, so the grounding is stable and the ask is not buried inside it.
    """
    a = (context or {}).get("assistant") or {}
    proj = (context or {}).get("project") or {}
    beats = (context or {}).get("my_recent_beats") or []
    out = []
    if docs:
        out.append(
            "# The product you are working on\n\n"
            "These are this project's OWN documents — its vision, mission and plan. They are "
            "the ground truth about what it is for and who it is for. Everything you build "
            "is judged against them.\n\n" + docs)
    out.append(
        "# Who you are\n\n"
        f"You are the **{a.get('role') or 'engineer'}** for this project, working inside its "
        f"git checkout. Your persona and standards:\n\n"
        + (a.get("soul_md") or "").strip())
    out.append(
        "# The app you are editing\n\n"
        f"- Live at: {proj.get('url') or '(not deployed)'}\n"
        f"- Original brief: {(proj.get('brief') or '')[:600]}\n"
        f"- Stack: Node + Express (`server.js`), Postgres, Redis. Migrations are plain SQL "
        f"files in `migrations/`, applied automatically at boot by `db/migrate.js`.\n"
        f"- Static assets are served from `public/`.\n"
        f"- `DATABASE_URL` and `REDIS_URL` are already in the environment. `APP_SECRET` is "
        f"available for signing.\n"
        + (f"\n## The repository right now\n\n{survey[:4000]}" if survey else ""))
    if beats:
        out.append("# What you did on recent beats\n\n" + "\n".join(
            f"- {b.get('ts', '')[:16]} [{b.get('status')}] {(b.get('thought') or '')[:220]}"
            for b in beats[:3]))
    out.append(
        "# House rules for this repository — these are not negotiable\n\n"
        "- **Never leave the app unable to start.** It is LIVE and it has real data. A change "
        "that does not boot is worse than no change: it will be rolled back automatically and "
        "your beat will be recorded as a failure.\n"
        "- **Never add a paid or external third-party service.** No auth SaaS, no hosted "
        "email, no external API. Everything is self-hosted inside this app's own Node + "
        "Postgres + Redis stack. Node's built-in `crypto` is available and is the right tool "
        "for hashing and signing.\n"
        "- **Never use string interpolation to build SQL.** Parameterised queries only "
        "(`$1`, `$2`). Migrations must be idempotent (`IF NOT EXISTS`) and additive — never "
        "edit an existing migration file, add a new numbered one.\n"
        "- **Do not touch** `db/migrate.js`, `Dockerfile`, `docker-compose.yml`, `.env`, or "
        "anything under `docs/assistants/`. The platform owns those; a change to them is "
        "rejected and your whole beat's work is thrown away.\n"
        "- **Do not run git commands.** Do not commit, do not push, do not branch, do not "
        "stash. Edit the working tree and stop; committing is handled for you afterwards.\n"
        "- Prefer NO new npm dependencies. If one is genuinely unavoidable, add it to "
        "`package.json` (the image installs with `npm install`, so no lockfile edit is "
        "needed).\n"
        "- Keep the change SMALL and COMPLETE. One coherent slice that actually works "
        "end-to-end beats three half-wired ones. Existing endpoints keep their existing "
        "response shapes unless the task says otherwise.\n"
        "- When you are finished, print a short summary of what you changed and why.\n")
    return "\n\n---\n\n".join(out)


# --- translating Pi's own event stream into activity lines -------------------------------
# This is the EXACT event vocabulary `pi --mode json` emits (verified by running 0.84.2
# against a stub OpenAI endpoint and capturing stdout):
#
#   session, agent_start, turn_start, turn_end, agent_end, agent_settled
#   message_start / message_end          role = user | assistant | toolResult
#   message_update                       .assistantMessageEvent.type =
#                                          text_start | text_delta | text_end |
#                                          toolcall_start | toolcall_delta | toolcall_end
#   tool_execution_start                 {toolCallId, toolName, args}
#   tool_execution_end                   {toolCallId, toolName, result, isError}
#   compaction_start / compaction_end, auto_retry_start / auto_retry_end, bash_execution_update
#
# We report the ones a human would care about and drop the token-level deltas. Pi's built-in
# tools are exactly `read`, `bash`, `edit`, `write` — there is no separate search tool, so a
# search shows up as a `bash` line running ripgrep, and that is what we display. We do not
# dress it up as something else.
_TOOL_ICON = {"read": "👀", "write": "✎", "edit": "✎", "bash": "$"}


def _tool_line(name: str, args: dict) -> tuple[str, str]:
    """(text, detail) for a tool call, from what Pi actually passed."""
    args = args if isinstance(args, dict) else {}
    path = str(args.get("path") or args.get("file_path") or args.get("filePath") or "")
    if name == "read":
        return (f"reading {path}" if path else "reading a file"), ""
    if name == "write":
        return (f"writing {path}" if path else "writing a file"), ""
    if name == "edit":
        return (f"editing {path}" if path else "editing a file"), ""
    if name == "bash":
        cmd = str(args.get("command") or args.get("cmd") or "").strip()
        return (cmd[:140] or "running a command"), ""
    return f"{name}({', '.join(sorted(args)[:3])})", ""


def _on_pi_event(ev: dict, seen: dict) -> None:
    t = ev.get("type")
    if t == "tool_execution_start":
        name = str(ev.get("toolName") or "tool")
        text, detail = _tool_line(name, ev.get("args") or {})
        seen["tools"] = seen.get("tools", 0) + 1
        activity("tool", _TOOL_ICON.get(name, "•"), text, detail)
    elif t == "tool_execution_end":
        if ev.get("isError"):
            res = ev.get("result") or {}
            body = ""
            for c in (res.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "text":
                    body += str(c.get("text") or "")
            activity("result", "✗", f"{ev.get('toolName')} failed", body[:400], ok=False)
    elif t == "message_update":
        ame = ev.get("assistantMessageEvent") or {}
        if ame.get("type") == "text_end":
            # The agent's own words at the end of a turn — its reasoning, verbatim.
            content = str(ame.get("content") or "").strip()
            if content:
                activity("text", "💬", content[:300],
                         content[300:1200] if len(content) > 300 else "")
    elif t == "auto_retry_start":
        activity("phase", "⟳", "the model call failed — retrying")
    elif t == "compaction_start":
        activity("phase", "🗜", "context is full — compacting the conversation")
    elif t == "agent_end":
        activity("phase", "✓", "coding agent finished", flush=True)


def run_pi(task: str, grounding: str) -> dict:
    """Run ONE non-interactive Pi session over the checkout, STREAMING its events out.

    `--mode json` (not `-p`) is what makes the /builder pane live: Pi writes one JSON event
    per line to stdout as it works, and we translate each into an activity line the moment
    it arrives. With `-p` the only output is the final message, and the user would stare at
    a frozen pane for two minutes — which is precisely the complaint this fixes.

    Flags, and why each one:
      --mode json         line-delimited event stream (implies non-interactive)
      --offline           no version check, no telemetry, no remote model-catalog fetch —
                          this container's egress is the control plane and Gitea only
      --no-session        write no session file; the beat is the unit of memory
      --no-approve        ignore project-local `.pi/` settings and extensions. The repository
                          is written by an LLM; letting it configure the agent that edits it
                          next is a self-modification loop we do not want
      --no-extensions/--no-skills/--no-prompt-templates
                          same reasoning, for discovery of anything outside this image
      --no-context-files  AGENTS.md / CLAUDE.md discovery off: WE decide the grounding
                          (the project's real docs), it is not scavenged from the tree
      --append-system-prompt  the grounding block, appended to Pi's own coding prompt so its
                          tool instructions stay intact
    A hard wall-clock timeout wraps the lot, because Pi has no step or turn cap of its own
    (confirmed against 0.84.2: no --max-turns, no cost limit). The other cap is the per-beat
    spend limit enforced at the control plane's LLM proxy.
    """
    write_pi_config()
    prompt_path = "/tmp/pi-grounding.md"
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(grounding)
    cmd = [
        "pi", "--mode", "json", "--offline", "--no-session", "--no-approve",
        "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files",
        "--model", PI_MODEL,
        "--append-system-prompt", prompt_path,
        task,
    ]
    env = {**os.environ, "HOME": "/tmp", "PI_CODING_AGENT_DIR": PI_DIR,
           "ASSISTANT_TOKEN": TOKEN, "BEAT_ID": BEAT_ID,
           "PI_OFFLINE": "1", "PI_TELEMETRY": "0", "NO_COLOR": "1"}
    t0 = time.monotonic()
    seen: dict = {}
    tail: list[str] = []
    said: list[str] = []
    try:
        proc = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env, bufsize=1)
    except FileNotFoundError:
        return {"ok": False, "rc": 127, "seconds": 0, "output": "pi is not installed",
                "tools": 0, "said": ""}
    deadline = t0 + PI_TIMEOUT_SEC
    timed_out = False
    try:
        for line in proc.stdout:                       # blocks per LINE, not per read()
            line = line.strip()
            if line:
                tail.append(line[:2000])
                del tail[:-40]
            if line.startswith("{"):
                try:
                    ev = json.loads(line)
                except Exception:
                    ev = None
                if isinstance(ev, dict):
                    try:
                        _on_pi_event(ev, seen)
                    except Exception as e:            # a feed bug must never kill the beat
                        print("[activity] " + str(e)[:200], flush=True)
                    if (ev.get("type") == "message_update"
                            and (ev.get("assistantMessageEvent") or {}).get("type")
                            == "text_end"):
                        said.append(str(
                            (ev.get("assistantMessageEvent") or {}).get("content") or ""))
            if time.monotonic() > deadline:
                timed_out = True
                activity("phase", "⏱", f"coding agent hit its {PI_TIMEOUT_SEC}s time limit "
                                       f"— stopping it", flush=True)
                proc.kill()
                break
    finally:
        try:
            proc.wait(timeout=30)
        except Exception:
            proc.kill()
        flush_activity()
    rc = 124 if timed_out else (proc.returncode if proc.returncode is not None else 1)
    secs = int(time.monotonic() - t0)
    # rc 124 is our timeout; Pi itself exits non-zero when the turn ended in an error. Either
    # way the tree may hold partial work — the gate below decides what happens to it, so a
    # non-zero rc is reported but is NOT on its own a reason to throw the change away.
    return {"ok": rc == 0, "rc": rc, "seconds": secs, "tools": seen.get("tools", 0),
            "said": redact(" ".join(said))[-1500:],
            "output": redact("\n".join(tail))[-4000:]}


# ---------------------------------------------------------------------------
# 3. THE GATE — what Pi produced, before anything is committed
# ---------------------------------------------------------------------------
def changed_files() -> list[str]:
    rc, out = sh(["git", "status", "--porcelain"])
    if rc != 0:
        return []
    files = []
    for line in out.splitlines():
        if len(line) > 3:
            path = line[3:].strip()
            if " -> " in path:              # a rename: the destination is what matters
                path = path.split(" -> ", 1)[1]
            files.append(path.strip('"'))
    return files


def discard_changes() -> None:
    sh(["git", "reset", "--hard", "HEAD"])
    sh(["git", "clean", "-fd"])


def gate_changes() -> dict:
    """Inspect what the coding agent actually did and decide whether it may be committed.

    Pi is free-form by design, so this is where the bounds live. A violation discards the
    WHOLE beat's work rather than committing the acceptable part of it: a half-applied change
    is how you get an app that starts but is subtly wrong, which is worse than no change.
    """
    files = changed_files()
    if not files:
        return {"ok": False, "empty": True, "files": [],
                "detail": "the coding agent changed nothing"}
    for f in files:
        if f in PROTECTED or any(f.startswith(p) for p in PROTECTED_PREFIXES):
            return {"ok": False, "files": files,
                    "detail": f"refused: `{f}` is a protected platform file — "
                              f"the whole change was discarded"}
        if f.startswith("../") or os.path.isabs(f):
            return {"ok": False, "files": files,
                    "detail": f"refused: `{f}` is outside the repository"}
    if len(files) > MAX_CHANGED_FILES:
        return {"ok": False, "files": files,
                "detail": f"refused: {len(files)} files changed in one beat "
                          f"(the cap is {MAX_CHANGED_FILES}) — that is a rewrite, not a change"}
    rc, out = sh(["git", "add", "-A"])
    rc, diff = sh(["git", "diff", "--cached", "--numstat"])
    added = 0
    rc2, stat = sh(["git", "diff", "--cached", "--shortstat"])
    rc3, raw = sh(["git", "diff", "--cached"], timeout=120)
    if len(raw) > MAX_DIFF_BYTES:
        sh(["git", "reset"])
        return {"ok": False, "files": files,
                "detail": f"refused: the diff is {len(raw)} bytes "
                          f"(the cap is {MAX_DIFF_BYTES})"}
    # A syntactically broken .js must never reach a deploy — the same rule the build pipeline
    # applies. This is cheap (one node --check per file) and catches the single most common
    # way an unattended edit takes an app down.
    for f in files:
        if f.endswith(".js") and os.path.isfile(os.path.join(REPO, f)):
            rc, out = sh(["node", "--check", f])
            if rc != 0:
                return {"ok": False, "files": files,
                        "detail": f"refused: `{f}` does not parse — node --check said:\n"
                                  + out[-400:]}
    return {"ok": True, "files": files, "stat": stat.strip()[:300],
            "diff_bytes": len(raw)}


# ---------------------------------------------------------------------------
# 4. SHIP — commit, push, and ask the control plane to deploy HEAD
# ---------------------------------------------------------------------------
def commit_and_push(message: str) -> dict:
    ok, why = granted("commit_push")
    if not ok:
        return {"ok": False, "denied": True, "detail": why}
    if not GIT_REMOTE:
        return {"ok": False, "detail": "no push remote available"}
    label = f"mikeos-assistant-{ASSISTANT_ID}"
    sh(["git", "config", "user.name", label])
    sh(["git", "config", "user.email", f"{label}@builderapps.osmike.com"])
    sh(["git", "add", "-A"])
    rc, out = sh(["git", "commit", "-m", message[:400]])
    if rc != 0:
        if "nothing to commit" in out.lower():
            return {"ok": True, "detail": "nothing to commit"}
        return {"ok": False, "detail": redact(out[-300:])}
    rc, sha = sh(["git", "rev-parse", "HEAD"])
    sha = sha.strip()[:40] if rc == 0 else ""
    # Never a force-push: this repo is the record of what the agent did.
    rc, out = sh(["git", "push", "origin", "HEAD:main"], timeout=180)
    if rc != 0:
        activity("result", "✗", "push failed", redact(out[-300:]), ok=False, flush=True)
        return {"ok": False, "sha": sha, "detail": "push failed: " + redact(out[-300:])}
    activity("result", "✓", f"committed {sha[:8]} “{message[:80]}”", ok=True,
             flush=True)
    return {"ok": True, "sha": sha, "detail": "committed and pushed", "message": message[:200]}


def ship_head(reason: str) -> dict:
    """Ask the control plane to build + health-gate HEAD, and WAIT for the verdict.

    Waiting is the point. A beat that reported success the moment it asked for a deploy would
    hide a health gate that went red ten minutes later — "never trust a 200", in its most
    expensive form. The control plane rolls back on failure; this reports which happened.
    """
    ok, why = granted("request_deploy")
    if not ok:
        return {"ok": False, "denied": True, "deployed": False,
                "detail": f"{why} — the commit is in git but was NOT deployed"}
    activity("phase", "🚀", "asking the control plane to build and health-gate this commit",
             flush=True)
    started = call("POST", "/api/assistant/act",
                   {"action": {"type": "request_deploy", "request": reason[:400]}},
                   timeout=120)
    if not started.get("ok"):
        activity("result", "✗", "the deploy was refused",
                 str(started.get("detail") or "")[:300], ok=False, flush=True)
        return {"ok": False, "deployed": False,
                "detail": str(started.get("detail") or "the deploy was refused")[:300]}
    run_id = started.get("run_id")
    log(f"ship: run {run_id} started; waiting up to {DEPLOY_WAIT_SEC}s for the health gate")
    deadline = time.monotonic() + DEPLOY_WAIT_SEC
    last: dict = {}
    announced: set = set()
    while time.monotonic() < deadline:
        time.sleep(8)
        try:
            last = call("GET", f"/api/assistant/deploy/{run_id}", timeout=60)
        except Exception as e:
            log("ship: status poll failed: " + redact(str(e))[:200])
            continue
        # Narrate the deploy's OWN steps as the control plane records them — so a rollback
        # is visible in the pane rather than silent.
        for s in (last.get("steps") or []):
            key = f"{s.get('name')}:{s.get('status')}"
            if key in announced or s.get("status") == "pending":
                continue
            announced.add(key)
            nice = {"ship_checkout": "checked out the pushed commit",
                    "ship_deploy": "built it and ran the health gate",
                    "ship_record": "recorded the deployment"}.get(
                        str(s.get("name")), str(s.get("name")))
            if s.get("status") == "running":
                activity("phase", "⏳", nice)
            elif s.get("status") == "done":
                activity("result", "✓", nice, str(s.get("detail") or "")[:300], ok=True)
            elif s.get("status") == "failed":
                activity("result", "✗", nice + " — FAILED",
                         str(s.get("detail") or "")[:400], ok=False)
        flush_activity()
        if last.get("finished"):
            break
    steps = last.get("steps") or []
    if not last.get("finished"):
        activity("phase", "⏱", f"still deploying after {DEPLOY_WAIT_SEC}s — the control "
                               f"plane carries on without me", flush=True)
        return {"ok": False, "deployed": False, "run_id": run_id, "timed_out": True,
                "detail": f"the deploy was still running after {DEPLOY_WAIT_SEC}s; it "
                          f"continues on the control plane and will roll back on its own "
                          f"if the health gate fails",
                "steps": [f"{s.get('name')}={s.get('status')}" for s in steps]}
    if last.get("status") == "done":
        activity("result", "🟢", "health gate green — the change is live", ok=True,
                 flush=True)
        return {"ok": True, "deployed": True, "run_id": run_id,
                "detail": next((s.get("detail") for s in steps
                                if s.get("name") == "ship_deploy"), "live")[:300]}
    activity("result", "🔴", "health gate FAILED — rolled back to the last good commit",
             str(last.get("error") or "")[:400], ok=False, flush=True)
    return {"ok": False, "deployed": False, "run_id": run_id,
            "detail": ("the health gate FAILED and the app was rolled back: "
                       + str(last.get("error") or "")[:400])}


def act_code(action: dict, context: dict, docs: str, survey: str) -> dict:
    """The whole coding act: brief Pi -> gate the diff -> commit+push -> ship -> report."""
    ok, why = granted("edit_code")
    if not ok:
        return {"ok": False, "denied": True, "detail": why}
    task = str(action.get("task") or action.get("text") or "").strip()
    if not task:
        return {"ok": False, "detail": "the `code` action needs a `task`"}
    if not os.path.isdir(os.path.join(REPO, ".git")):
        return {"ok": False, "detail": "no checkout to work in"}

    grounding = build_grounding(docs, context, survey)
    log(f"code: handing a {len(task)}-char task to Pi with a {len(grounding)}-char grounding")
    activity("phase", "🛠", f"starting on: {task[:160]}", flush=True)
    pi = run_pi(task, grounding)
    log(f"code: Pi exited rc={pi['rc']} after {pi['seconds']}s, {pi['tools']} tool call(s)")
    tail = pi["output"][-1200:]

    gate = gate_changes()
    if not gate["ok"]:
        discard_changes()
        activity("result", "✗", gate["detail"][:200],
                 ", ".join(gate.get("files", [])[:10]), ok=False, flush=True)
        return {"ok": False, "coded": False, "gate": gate["detail"],
                "files": gate.get("files", [])[:20], "pi_rc": pi["rc"],
                "pi_seconds": pi["seconds"], "pi_tool_calls": pi["tools"],
                "detail": gate["detail"], "pi_tail": tail}
    activity("result", "✎", f"changed {len(gate['files'])} file(s): "
                            + ", ".join(gate["files"][:6]),
             gate.get("stat", ""), ok=True)

    message = str(action.get("message") or "").strip() or f"feat: {task[:60]}"
    if not message.lower().startswith(("feat", "fix", "chore", "docs", "refactor", "test")):
        message = "feat: " + message
    pushed = commit_and_push(message)
    if not pushed.get("ok") or not pushed.get("sha"):
        return {"ok": False, "coded": True, "pushed": False,
                "files": gate["files"], "stat": gate.get("stat", ""),
                "detail": str(pushed.get("detail"))[:300], "pi_tail": tail}

    shipped = ship_head(f"assistant {ASSISTANT_ID}: {task[:200]}")
    return {"ok": bool(shipped.get("ok")), "coded": True, "pushed": True,
            "sha": pushed.get("sha", "")[:12], "files": gate["files"],
            "stat": gate.get("stat", ""), "commit_message": message[:200],
            "pi_seconds": pi["seconds"],
            "deployed": bool(shipped.get("deployed")),
            "detail": str(shipped.get("detail"))[:400], "pi_tail": tail}


def mirror_soul(context: dict) -> str:
    """Write the SOUL into the repo at `docs/assistants/<role>.SOUL.md`.

    The SOUL should live in git next to the app it serves — that is what makes it reviewable
    in a diff like everything else. Written by THIS program, deliberately, and listed as a
    protected path so the coding agent cannot edit its own persona.
    """
    if "commit_push" not in CAPS:
        return "soul mirror skipped (commit_push not granted)"
    a = (context or {}).get("assistant") or {}
    rel, body = a.get("soul_path"), a.get("soul_md")
    if not rel or not body:
        return "soul mirror skipped (nothing to write)"
    dest = os.path.realpath(os.path.join(REPO, str(rel).lstrip("/")))
    if not dest.startswith(os.path.realpath(REPO) + os.sep):
        return "soul mirror refused (path escapes the checkout)"
    try:
        if os.path.isfile(dest) and read_capped(dest) == body:
            return "soul already in the repo, unchanged"
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(body)
        return f"wrote {rel} ({len(body)} chars) — it ships with the next commit"
    except OSError as e:
        return "soul mirror failed: " + str(e)[:200]


# ---------------------------------------------------------------------------
# the beat
# ---------------------------------------------------------------------------
def main() -> int:
    t0 = time.monotonic()
    if not (CONTROL_URL and TOKEN and BEAT_ID):
        print("missing CONTROL_URL / ASSISTANT_TOKEN / BEAT_ID", file=sys.stderr)
        return 2

    status, thought, actions_out = "done", "", []
    tokens, cost = 0, 0.0
    try:
        log(f"beat {BEAT_ID} for assistant {ASSISTANT_ID} on project {PROJECT_ID} "
            f"({TRIGGER_KIND}); capabilities: {','.join(sorted(CAPS)) or 'none'}")

        # --- perceive -------------------------------------------------------
        activity("phase", "⟲", "refreshing my checkout of the repository", flush=True)
        log("perceive: " + refresh_checkout())
        docs, docnote = load_docs()                     # DETERMINISTIC, always, first
        log(f"perceive: documents — {docnote} ({len(docs)} chars)")
        activity("phase", "📖", "reading the project's documents — " + docnote[:220])
        survey = survey_workspace()
        log(f"perceive: workspace survey is {len(survey)} chars")
        context = call("GET", "/api/assistant/context", timeout=90)
        log("perceive: control plane context received")
        if os.path.isdir(os.path.join(REPO, ".git")):
            log("perceive: " + mirror_soul(context))

        # --- reason (ONE call; the control plane owns the model credential) --
        log("reason: asking the control plane to run one LLM round")
        activity("phase", "🤔", "deciding what is worth doing this beat", flush=True)
        r = call("POST", "/api/assistant/reason",
                 {"workspace": survey[:12000], "docs": docs[:40000]}, timeout=300)
        thought = str(r.get("thought") or "")
        planned = r.get("actions") or []
        tokens = int(r.get("tokens") or 0)
        cost = float(r.get("cost_usd") or 0.0)
        log(f"reason: {len(planned)} action(s) planned, {tokens} tokens, ${cost:.4f}")
        if thought:
            activity("text", "🧠", thought[:300],
                     thought[300:1500] if len(thought) > 300 else "", flush=True)

        # --- act ------------------------------------------------------------
        for action in planned[:2]:
            kind = str(action.get("type") or "").strip().lower()
            log(f"act: {kind}")
            if kind in ("code", "write_file", "edit"):
                result = act_code(action, context, docs, survey)
            elif kind == "commit_push":
                # Legacy shape: the coding act commits for itself now, but an agent that
                # asks for a bare commit should still get an honest answer.
                gate = gate_changes()
                result = (commit_and_push(str(action.get("message") or "chore: assistant"))
                          if gate["ok"] else {"ok": False, "detail": gate["detail"]})
            else:
                # Remote acts are authorised SERVER-SIDE. This container's own capability
                # list is a fail-fast convenience, not the security boundary.
                try:
                    result = call("POST", "/api/assistant/act", {"action": action},
                                  timeout=300)
                except Exception as e:
                    result = {"ok": False, "detail": redact(str(e))[:400]}
            log(f"act: {kind} -> {json.dumps(result)[:400]}")
            if kind in ("code", "write_file", "edit") and not result.get("ok"):
                # A coding act that did not end with a healthy deploy is a FAILED beat, not a
                # quiet one. The whole point of the health gate is that somebody is told.
                status = "failed"
            actions_out.append({"type": kind, "input": {
                k: (str(v)[:600] if isinstance(v, str) else v)
                for k, v in action.items()}, "result": result})

        if not planned:
            status = "skipped"
            log("act: nothing worth doing this beat")
            activity("phase", "💤", "nothing worth doing this beat", flush=True)
    except Exception as e:
        status = "failed"
        thought = thought or ""
        log("FAILED: " + redact(str(e))[:800])
        try:
            activity("result", "✗", "the beat failed", redact(str(e))[:400], ok=False,
                     flush=True)
        except Exception:
            pass
    finally:
        try:
            flush_activity()
        except Exception:
            pass
        # remember — ALWAYS. A beat that produced nothing must still leave a trace, or the
        # assistant "feels dead" for reasons nobody can see. (The control plane adds what the
        # coding agent spent through the LLM proxy — this program never sees that number.)
        try:
            call("POST", "/api/assistant/beat", {
                "status": status, "thought": thought, "actions": actions_out,
                "log": redact("\n".join(LOG))[-18000:], "tokens": tokens,
                "cost_usd": cost,
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }, timeout=60)
        except Exception as e:
            print("could not record the beat: " + redact(str(e)), file=sys.stderr)
            return 1
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
