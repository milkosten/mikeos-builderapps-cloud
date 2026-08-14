"""Workspace / checkout manager (phase 11).

Each project's repo is checked out to /opt/builderapps/workspaces/<id> (NVMe). A per-project
asyncio lock serializes runs so two pipelines can't stomp the same tree. Git identity comes
from the user's Gitea account. File reads are size-capped (never slurp a big blob into RAM).
"""
import asyncio
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from server import gitea

logger = logging.getLogger(__name__)

WORKSPACES_ROOT = Path(os.environ.get("WORKSPACES_ROOT", "/opt/builderapps/workspaces"))
_FILE_READ_CAP = 30 * 1024 * 1024  # 30 MB house-rule cap

_locks: dict[str, asyncio.Lock] = {}


def lock(project_id: str) -> asyncio.Lock:
    lk = _locks.get(project_id)
    if lk is None:
        lk = asyncio.Lock()
        _locks[project_id] = lk
    return lk


def path_for(project_id: str) -> Path:
    return WORKSPACES_ROOT / project_id


async def _run(cmd: list[str], cwd: Optional[Path] = None, env: Optional[dict] = None,
               timeout: float = 180.0) -> tuple[int, str]:
    """Run a subprocess, capturing combined output. Returns (rc, output)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"command timed out: {' '.join(cmd)}")
    return proc.returncode, (out or b"").decode("utf-8", "replace")


async def checkout(project_id: str, gitea_user: str, repo: str, token: str,
                   author_name: Optional[str] = None,
                   author_email: Optional[str] = None) -> Path:
    """Clone (token-over-HTTPS) if absent, else fetch + hard reset to origin/main.
    Sets a local git identity so the harness can commit as the user."""
    WORKSPACES_ROOT.mkdir(parents=True, exist_ok=True)
    dst = path_for(project_id)
    url = gitea.clone_url_for(gitea_user, repo, token)
    if not (dst / ".git").is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        rc, out = await _run(["git", "clone", "--depth", "1", url, str(dst)], timeout=180)
        if rc != 0:
            raise RuntimeError(f"git clone failed: {out[-500:]}")
    else:
        await _run(["git", "remote", "set-url", "origin", url], cwd=dst)
        rc, out = await _run(["git", "fetch", "origin"], cwd=dst)
        if rc != 0:
            raise RuntimeError(f"git fetch failed: {out[-500:]}")
        await _run(["git", "reset", "--hard", "origin/HEAD"], cwd=dst)

    name = author_name or gitea_user
    email = author_email or f"{gitea_user}@builderapps.osmike.com"
    await _run(["git", "config", "user.name", name], cwd=dst)
    await _run(["git", "config", "user.email", email], cwd=dst)
    return dst


async def commit_push(project_id: str, message: str,
                      gitea_user: str, repo: str, token: str) -> bool:
    """git add -A && commit && push. Returns False (no error) when nothing changed."""
    dst = path_for(project_id)
    await _run(["git", "add", "-A"], cwd=dst)
    rc, out = await _run(["git", "commit", "-m", message], cwd=dst)
    if rc != 0:
        if "nothing to commit" in out.lower():
            return False
        raise RuntimeError(f"git commit failed: {out[-500:]}")
    url = gitea.clone_url_for(gitea_user, repo, token)
    await _run(["git", "remote", "set-url", "origin", url], cwd=dst)
    rc, out = await _run(["git", "push", "origin", "HEAD:main"], cwd=dst, timeout=120)
    if rc != 0:
        raise RuntimeError(f"git push failed: {out[-500:]}")
    return True


async def head_sha(project_id: str) -> str:
    """The commit currently checked out, or "" if there is no checkout."""
    dst = path_for(project_id)
    if not (dst / ".git").is_dir():
        return ""
    rc, out = await _run(["git", "rev-parse", "HEAD"], cwd=dst, timeout=30)
    return out.strip() if rc == 0 else ""


async def commits_between(project_id: str, base_sha: str) -> list[str]:
    """`<base>..HEAD` as "<short> <subject>" lines — i.e. exactly what a rollback would undo."""
    dst = path_for(project_id)
    if not (dst / ".git").is_dir() or not base_sha:
        return []
    rc, out = await _run(["git", "log", "--pretty=format:%h %s", f"{base_sha}..HEAD"],
                         cwd=dst, timeout=30)
    if rc != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()][:20]


async def roll_back_to(project_id: str, good_sha: str, message: str,
                       gitea_user: str, repo: str, token: str) -> Optional[str]:
    """Put the repo's CONTENT back to `good_sha` with a new FORWARD commit, and push it.

    Three deliberate properties, because this runs unattended after an agent broke the app:

    * **Never a force-push, never a history rewrite.** The bad commit stays in the log where
      a human can read it; the fix is an ordinary commit on top that restores the last good
      tree. Rewriting an agent's mistake out of history would also destroy the evidence of
      what it did — the opposite of what an audit trail is for.
    * **It cannot conflict.** `git read-tree -u --reset <sha>` makes the index and working
      tree *equal* to that commit's tree outright; there is no merge and therefore no
      conflict to resolve, which matters when nobody is watching. (`git revert
      <good>..HEAD` can and does conflict, and a half-reverted tree would be worse than the
      broken one.)
    * **Ignored files survive.** `.env` and `docker-compose.normalized.yml` are not in the
      index, so the deployer's own generated state is untouched.

    Returns the new commit's sha, or None when there was nothing to roll back (HEAD already
    IS the good commit) or the rollback could not be completed.
    """
    dst = path_for(project_id)
    if not (dst / ".git").is_dir() or not good_sha:
        return None
    rc, cur = await _run(["git", "rev-parse", "HEAD"], cwd=dst, timeout=30)
    if rc == 0 and cur.strip().startswith(good_sha[:7]):
        return None                                   # already there — nothing to undo
    rc, out = await _run(["git", "cat-file", "-e", f"{good_sha}^{{commit}}"], cwd=dst,
                         timeout=30)
    if rc != 0:
        # A shallow clone may simply not have the good commit. Deepen once, then give up
        # loudly rather than "rolling back" to something we cannot see.
        await _run(["git", "fetch", "--unshallow"], cwd=dst, timeout=180)
        rc, _ = await _run(["git", "cat-file", "-e", f"{good_sha}^{{commit}}"], cwd=dst,
                           timeout=30)
        if rc != 0:
            logger.error("rollback target %s is not in %s's checkout", good_sha, project_id)
            return None
    rc, out = await _run(["git", "read-tree", "-u", "--reset", good_sha], cwd=dst, timeout=60)
    if rc != 0:
        logger.error("rollback read-tree failed for %s: %s", project_id, out[-300:])
        return None
    rc, out = await _run(["git", "commit", "-m", message[:400]], cwd=dst, timeout=60)
    if rc != 0:
        if "nothing to commit" in out.lower():
            return None
        logger.error("rollback commit failed for %s: %s", project_id, out[-300:])
        return None
    url = gitea.clone_url_for(gitea_user, repo, token)
    await _run(["git", "remote", "set-url", "origin", url], cwd=dst)
    rc, out = await _run(["git", "push", "origin", "HEAD:main"], cwd=dst, timeout=120)
    if rc != 0:
        # The app is restored locally and will be redeployed either way; a push failure is
        # recorded, not raised, so we never leave the box running code we could not restore.
        logger.error("rollback push failed for %s: %s", project_id, out[-300:])
    return (await _run(["git", "rev-parse", "HEAD"], cwd=dst, timeout=30))[1].strip()


async def revert_uncommitted(project_id: str) -> bool:
    """Throw away everything since the last commit (phase 28).

    A feature step that fails twice leaves half-written, non-building code in the tree. If we
    just skipped it, the NEXT feature would inherit the wreckage and fail too, and the final
    deploy would ship it. Every good state is a commit (the pipeline commits per feature), so
    `reset --hard HEAD` + `clean -fd` is exactly "put the app back the way it worked".
    Returns True if anything was discarded. Never raises — it is a cleanup path.
    """
    dst = path_for(project_id)
    if not (dst / ".git").is_dir():
        return False
    try:
        rc, out = await _run(["git", "status", "--porcelain"], cwd=dst, timeout=60)
        dirty = bool(rc == 0 and out.strip())
        await _run(["git", "reset", "--hard", "HEAD"], cwd=dst, timeout=60)
        # -fd, never -x: the deployer's generated docker-compose.normalized.yml and other
        # ignored artifacts are not the agent's broken work.
        await _run(["git", "clean", "-fd"], cwd=dst, timeout=60)
        if dirty:
            logger.info("reverted uncommitted work in %s", project_id)
        return dirty
    except Exception as e:  # noqa: BLE001
        logger.warning("revert_uncommitted failed for %s: %s", project_id, e)
        return False


async def recent_commits(project_id: str, n: int = 8) -> list[str]:
    """Return the last N commit subjects (for the update context block). Best-effort."""
    dst = path_for(project_id)
    if not (dst / ".git").is_dir():
        return []
    rc, out = await _run(["git", "log", f"-{n}", "--pretty=format:%s"], cwd=dst, timeout=30)
    if rc != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def read_file_capped(project_id: str, relpath: str) -> Optional[str]:
    """Read a workspace file, refusing anything over the size cap (RAM house rule)."""
    p = (path_for(project_id) / relpath).resolve()
    root = path_for(project_id).resolve()
    if not str(p).startswith(str(root)):
        raise ValueError("path escapes workspace")
    if not p.is_file():
        return None
    if p.stat().st_size > _FILE_READ_CAP:
        raise ValueError(f"file {relpath} exceeds {_FILE_READ_CAP} byte cap")
    return p.read_text("utf-8", "replace")


def write_file(project_id: str, relpath: str, content: str) -> None:
    p = (path_for(project_id) / relpath).resolve()
    root = path_for(project_id).resolve()
    if not str(p).startswith(str(root)):
        raise ValueError("path escapes workspace")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, "utf-8")


# ---- the build placeholder ------------------------------------------------
# The template ships `public/index.html` as a "your app is being built" holding page so the
# subdomain shows something friendly during the ~20 minutes of the build. `express.static` is
# mounted BEFORE the app's own routes, so if the agent writes a server-rendered `app.get("/")`
# and never touches that file, the finished product serves the holding page forever — the
# public home page of the campaign's changelog site was the builder's placeholder, and both the
# health gate (which only reads /health) and `finalize` ("14 of 14 features built") called it a
# success.
_PLACEHOLDER_MARK = "Your app is being built"
_ROOT_ROUTE_RE = re.compile(r"""app\.(get|use)\(\s*["']/["']""")


def _serves_own_root(project_id: str) -> bool:
    """True when the app defines its own `/` handler (so the static file is shadowing it)."""
    try:
        src = read_file_capped(project_id, "server.js") or ""
    except Exception:  # noqa: BLE001
        return False
    return bool(_ROOT_ROUTE_RE.search(src))


def placeholder_state(project_id: str) -> str:
    """`gone` | `shadowing` | `only` — what the build holding page is doing to this app."""
    p = path_for(project_id) / "public" / "index.html"
    if not p.is_file():
        return "gone"
    try:
        if _PLACEHOLDER_MARK not in p.read_text("utf-8", "replace"):
            return "gone"
    except OSError:
        return "gone"
    return "shadowing" if _serves_own_root(project_id) else "only"


def drop_placeholder(project_id: str) -> None:
    (path_for(project_id) / "public" / "index.html").unlink(missing_ok=True)
    logger.info("removed the build placeholder shadowing / in %s", project_id)


def cleanup(project_id: str) -> None:
    dst = path_for(project_id)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
