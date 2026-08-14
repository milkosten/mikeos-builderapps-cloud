#!/usr/bin/env python3
"""ONE beat of ONE assistant. The container is the beat; when this exits, the beat is over.

    perceive  -> refresh the checkout, look at it with real tools, ask the control plane
                 for the project's state
    reason    -> ONE LLM call (made BY the control plane, so no model key ever lives in this
                 container): SOUL + role + context -> {thought, actions[], done}
    act       -> execute only what this assistant was granted; the control plane re-checks
                 every capability, so a tampered container gains nothing
    remember  -> POST the beat record (thought + actions + tokens + cost) back

Stdlib only, on purpose: the beat program must not need a package install (or a network trip
to a registry) to start, and a smaller dependency surface inside an LLM-driven container is
worth more than convenience.
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

# House rule: never slurp a file into RAM without a cap.
FILE_CAP = 200 * 1024
LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG.append(line)
    print(line, flush=True)


def redact(s: str) -> str:
    """A token must never reach a log line or a beat record."""
    s = re.sub(r"asst_[A-Za-z0-9_\-]{8,}", "asst_***", s or "")
    return re.sub(r"://[^/@\s]+:[^/@\s]+@", "://***:***@", s)


# ---------------------------------------------------------------------------
# control-plane client
# ---------------------------------------------------------------------------
def call(method: str, path: str, body=None, timeout: float = 240.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        CONTROL_URL + path, data=data, method=method,
        headers={"X-Assistant-Token": TOKEN, "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {redact(detail)}") from None
    except Exception as e:
        raise RuntimeError(f"{method} {path} failed: {redact(str(e))}") from None
    return json.loads(raw) if raw.strip() else {}


# ---------------------------------------------------------------------------
# shell helper — real tools, bounded
# ---------------------------------------------------------------------------
def sh(args: list[str], cwd: str = REPO, timeout: int = 90) -> tuple[int, str]:
    try:
        p = subprocess.run(args, cwd=cwd if os.path.isdir(cwd) else None,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))[:20000]
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(args[:3])}"
    except FileNotFoundError:
        return 127, f"not installed: {args[0]}"


# ---------------------------------------------------------------------------
# 1. PERCEIVE — refresh the checkout and look at it
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


def read_capped(path: str) -> str:
    """Never load a whole file into RAM (the 1.55 GB video lesson, scaled down)."""
    try:
        if os.path.getsize(path) > FILE_CAP:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(FILE_CAP) + "\n… [truncated]"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def survey_workspace() -> str:
    """What the checkout looks like right now, gathered with the real tools this image ships.

    This is the whole point of running in a container instead of calling an LLM from the
    control plane: `git log`, `ripgrep` and the file tree are how a colleague forms an
    opinion, and they are all right here."""
    if not os.path.isdir(os.path.join(REPO, ".git")):
        return ""
    parts: list[str] = []

    rc, out = sh(["git", "log", "-12", "--pretty=format:%h %ad %s", "--date=short"])
    if rc == 0 and out.strip():
        parts.append("## Recent commits\n" + out.strip()[:2500])

    rc, out = sh(["git", "ls-files"])
    if rc == 0:
        files = [f for f in out.splitlines() if f.strip()]
        parts.append(f"## Tracked files ({len(files)})\n" + "\n".join(sorted(files)[:200]))

    for doc in ("README.md", "package.json", "docs/VISION.md", "docs/TECH-PLAN.md"):
        body = read_capped(os.path.join(REPO, doc))
        if body:
            parts.append(f"## {doc}\n{body[:2500]}")

    # ripgrep, not a python walk: it respects .gitignore and never reads a blob into RAM.
    rc, out = sh(["rg", "-n", "--max-count", "3", "-e", "TODO", "-e", "FIXME", "-e", "XXX",
                  "--glob", "!node_modules", "."])
    if rc == 0 and out.strip():
        parts.append("## TODO / FIXME markers\n" + out.strip()[:1500])

    rc, out = sh(["git", "diff", "--stat", "HEAD~5..HEAD"])
    if rc == 0 and out.strip():
        parts.append("## What changed over the last 5 commits\n" + out.strip()[:1200])

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 3. ACT — local actions (the ones that need the checkout); the rest go to the control plane
# ---------------------------------------------------------------------------
def act_write_file(action: dict) -> dict:
    if "edit_code" not in CAPS:
        return {"ok": False, "denied": True, "detail": "edit_code not granted"}
    rel = str(action.get("path") or "").strip().lstrip("/")
    content = action.get("content")
    if not rel or not isinstance(content, str):
        return {"ok": False, "detail": "write_file needs `path` and string `content`"}
    dest = os.path.realpath(os.path.join(REPO, rel))
    if not dest.startswith(os.path.realpath(REPO) + os.sep):
        return {"ok": False, "detail": "path escapes the checkout"}
    if len(content) > 400_000:
        return {"ok": False, "detail": "refusing to write more than 400 KB in one action"}
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(content)
    # A syntactically broken .js is rejected here rather than pushed (the pipeline's rule).
    if dest.endswith(".js"):
        rc, out = sh(["node", "--check", dest])
        if rc != 0:
            sh(["git", "checkout", "--", rel])
            return {"ok": False, "detail": "rejected: node --check failed\n" + out[-400:]}
    return {"ok": True, "path": rel, "bytes": len(content)}


def act_commit_push(action: dict) -> dict:
    if "commit_push" not in CAPS:
        return {"ok": False, "denied": True, "detail": "commit_push not granted"}
    if not GIT_REMOTE:
        return {"ok": False, "detail": "no push remote available"}
    msg = str(action.get("message") or "assistant: routine update").strip()[:200]
    label = f"mikeos-assistant-{ASSISTANT_ID}"
    sh(["git", "config", "user.name", label])
    sh(["git", "config", "user.email", f"{label}@builderapps.osmike.com"])
    sh(["git", "add", "-A"])
    rc, out = sh(["git", "commit", "-m", msg])
    if rc != 0:
        if "nothing to commit" in out.lower():
            return {"ok": True, "detail": "nothing to commit"}
        return {"ok": False, "detail": redact(out[-300:])}
    rc, out = sh(["git", "push", "origin", "HEAD:main"], timeout=180)
    if rc != 0:
        return {"ok": False, "detail": "push failed: " + redact(out[-300:])}
    return {"ok": True, "detail": "committed and pushed", "message": msg}


LOCAL_ACTIONS = {"write_file": act_write_file, "commit_push": act_commit_push}


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

        # --- perceive ---
        log("perceive: " + refresh_checkout())
        survey = survey_workspace()
        log(f"perceive: workspace survey is {len(survey)} chars")
        context = call("GET", "/api/assistant/context", timeout=90)
        log("perceive: control plane context received")

        # --- reason (ONE call; the control plane owns the model credential) ---
        log("reason: asking the control plane to run one LLM round")
        r = call("POST", "/api/assistant/reason", {"workspace": survey[:12000]}, timeout=300)
        thought = str(r.get("thought") or "")
        planned = r.get("actions") or []
        tokens = int(r.get("tokens") or 0)
        cost = float(r.get("cost_usd") or 0.0)
        log(f"reason: {len(planned)} action(s) planned, {tokens} tokens, ${cost:.4f}")

        # --- act ---
        for action in planned[:2]:
            kind = str(action.get("type") or "").strip().lower()
            log(f"act: {kind}")
            if kind in LOCAL_ACTIONS:
                result = LOCAL_ACTIONS[kind](action)
            else:
                # Remote acts are authorised SERVER-SIDE. This container's own capability
                # list is a fail-fast convenience, not the security boundary.
                try:
                    result = call("POST", "/api/assistant/act", {"action": action},
                                  timeout=300)
                except Exception as e:
                    result = {"ok": False, "detail": redact(str(e))[:400]}
            log(f"act: {kind} -> {json.dumps(result)[:300]}")
            actions_out.append({"type": kind, "input": {
                k: (str(v)[:400] if isinstance(v, str) else v)
                for k, v in action.items() if k != "content"}, "result": result})

        if not planned:
            status = "skipped"
            log("act: nothing worth doing this beat")
    except Exception as e:
        status = "failed"
        thought = thought or ""
        log("FAILED: " + redact(str(e))[:800])
    finally:
        # remember — ALWAYS. A beat that produced nothing must still leave a trace, or the
        # assistant "feels dead" for reasons nobody can see.
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
