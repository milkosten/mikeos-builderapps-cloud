"""Assistant runtime (phase 29) — the scheduler, the beat container, and the act surface.

## The shape of a beat

    scheduler (here)  ->  docker run --rm mikeos-assistant-runtime   (one beat, then exits)
                              |  clone/refresh its own workspace with real git
                              |  perceive: tree, commits, README, health, QA, cost
                              |  POST /api/assistant/reason   -> {thought, actions[], done}
                              |  act, each action capability-checked SERVER-SIDE
                              '- POST /api/assistant/beat     -> the durable beat record

Ephemeral per beat, not a long-lived pet: 242 already runs ~140 containers and every project
app is three of them, so idle assistant containers would not scale — and a fresh container
per beat means no drifting state. A per-assistant workspace directory keeps the clone cheap.

## The safety boundary (the part that must never be softened)

**An assistant container NEVER gets the Docker socket.** It may read, edit, commit and push
its own project repo — but to DEPLOY it asks the control plane (`act_request_deploy` ->
`server.shipper`), which builds the pushed HEAD through the compose normalizer and the
health gate, and rolls back to the last good commit if the gate goes red. Docker-socket
access is host root; handing that to an LLM-driven container would make every isolation
guarantee in this platform meaningless. It is the same reason project apps never get it.

Note what `request_deploy` is NOT: it is not the build pipeline's update path. Assistants are
disconnected from that pipeline — running it here would re-plan a minimal diff with a second
LLM and overwrite the change the assistant just pushed. It means "ship the current HEAD".

Alongside that: `cap_drop ALL`, `no-new-privileges`, a non-root uid, memory/cpu/pids caps, a
read-only rootfs with a tmpfs for scratch, and a per-assistant credential scoped to exactly
{project, assistant} that the beat holds for one beat.

## What enforces capabilities

`server.assistants.require()` — called on the CONTROL PLANE for every act. The beat program
also checks locally (fail fast, clearer logs), but a modified container cannot talk its way
past the server-side check, which is the one that counts.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import time
from typing import Any, Optional

from server import assistants as A
from server import budget, chrome, gitea, gpu, runner, store, usage, ws_hub
from server import messaging as M
from server import workspace_store as W
from server.harness import codegen

logger = logging.getLogger(__name__)

IMAGE = os.environ.get("ASSISTANT_IMAGE", "mikeos-assistant-runtime:latest")
# Build context for that image. Mounted into the control plane at the SAME host path (the
# established pattern here) so `docker build` streams a context the host daemon can resolve.
RUNTIME_CONTEXT = os.environ.get(
    "ASSISTANT_RUNTIME_CONTEXT", "/opt/mikeos-builderapps-cloud/assistant-runtime")
# Per-assistant workspaces. Same path both sides, like /opt/builderapps/workspaces.
ASSISTANT_ROOT = os.environ.get("ASSISTANT_WORKSPACES_ROOT", "/opt/builderapps/assistants")
NETWORK = os.environ.get("DEPLOY_NETWORK", "deploy_default")
CONTROL_URL = os.environ.get("ASSISTANT_CONTROL_URL", "http://mikeos-builderapps:8000")

# A beat that CODES is legitimately long: perceive (~20s) + one reasoning round (~25s) +
# a full Pi session (up to 780s) + commit/push + waiting out a docker build and the health
# gate (up to 900s). 420s was right when a beat could only comment. The container enforces
# its own inner limits (PI_TIMEOUT_SEC, DEPLOY_WAIT_SEC); this is the outer wall.
BEAT_TIMEOUT_SEC = int(os.environ.get("ASSISTANT_BEAT_TIMEOUT_SEC", "2400"))
SCHED_INTERVAL_SEC = float(os.environ.get("ASSISTANT_SCHED_SEC", "60"))
ENABLED = os.environ.get("ASSISTANTS_ENABLED", "1").strip() not in ("0", "false", "no")
# The container runs as this uid; its workspace dir is chowned to match.
RUN_UID = int(os.environ.get("ASSISTANT_UID", "10001"))

# Node + a coding agent holding a repo's worth of context needs more headroom than the
# comment-only beat did; an OOM-killed beat looks like a mysterious rc=137 with no report.
MEM_LIMIT = os.environ.get("ASSISTANT_MEM", "2g")
CPU_LIMIT = os.environ.get("ASSISTANT_CPUS", "1")
PIDS_LIMIT = os.environ.get("ASSISTANT_PIDS", "256")

# ONE beat container at a time across the whole box. Assistants are background work and must
# never compete with a customer watching their build.
_beat_slot = asyncio.Semaphore(1)
_sched_task: Optional[asyncio.Task] = None
_shutting_down = False
_image_ready = False
_image_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# subprocess helper
# ---------------------------------------------------------------------------
async def _run(cmd: list[str], timeout: float = 120.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, (out or b"").decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# the runtime image
# ---------------------------------------------------------------------------
async def ensure_image() -> None:
    """Build `mikeos-assistant-runtime` once, on first use. Cheap to re-check (an image
    inspect), and self-healing after a `docker image prune` took it away."""
    global _image_ready
    if _image_ready:
        return
    async with _image_lock:
        if _image_ready:
            return
        rc, _ = await _run(["docker", "image", "inspect", IMAGE], timeout=30)
        if rc == 0:
            _image_ready = True
            return
        if not os.path.isdir(RUNTIME_CONTEXT):
            raise RuntimeError(
                f"assistant runtime image {IMAGE} is missing and its build context "
                f"{RUNTIME_CONTEXT} is not mounted — "
                f"build it on the host: docker build -t {IMAGE} <repo>/assistant-runtime")
        logger.info("building assistant runtime image %s from %s", IMAGE, RUNTIME_CONTEXT)
        rc, out = await _run(["docker", "build", "-t", IMAGE, RUNTIME_CONTEXT], timeout=900)
        if rc != 0:
            raise RuntimeError(f"assistant runtime image build failed: {out[-1500:]}")
        _image_ready = True


# ---------------------------------------------------------------------------
# launching one beat
# ---------------------------------------------------------------------------
def _workspace_dir(assistant_id: int) -> str:
    return os.path.join(ASSISTANT_ROOT, str(int(assistant_id)))


def _prepare_workspace(assistant_id: int) -> str:
    """The assistant's own checkout dir, owned by the unprivileged container uid.

    Deliberately NOT the pipeline's `/opt/builderapps/workspaces/<id>` tree: a beat must
    never race a live build over the same git checkout."""
    d = _workspace_dir(assistant_id)
    os.makedirs(d, exist_ok=True)
    try:
        os.chown(d, RUN_UID, RUN_UID)
    except PermissionError:  # not root (local dev) — the container will fall back to /tmp
        logger.debug("could not chown %s to %s", d, RUN_UID)
    return d


def cleanup_workspace(assistant_id: int) -> None:
    shutil.rmtree(_workspace_dir(assistant_id), ignore_errors=True)


async def _git_remote_for(project: dict, user_id: str) -> str:
    """Tokenised clone URL for the project's repo, or "" when we cannot mint one.

    HONEST LIMITATION (v1): Gitea tokens are per-USER, not per-repo, so this is the owner's
    token — an assistant could in principle touch another repo *of the same owner*. Tenant
    isolation (the property that actually matters) is intact; true per-repo scoping needs a
    per-repo deploy key and is tracked as follow-up work.
    """
    acct = await store.get_gitea_account(user_id)
    if not acct:
        return ""
    return gitea.clone_url_for(acct["gitea_username"], project.get("gitea_repo", ""),
                               acct["token"])


async def launch_beat(assistant: dict, beat_id: int, *, trigger_kind: str) -> tuple[int, str]:
    """Run ONE beat container to completion. Returns (rc, combined output).

    Hardened per the phase-21 set. Note what is NOT here: no `-v /var/run/docker.sock`, no
    `--privileged`, no host bind mount other than the assistant's own workspace.
    """
    await ensure_image()
    aid = int(assistant["id"])
    project_id = str(assistant["project_id"])
    proj = await store.get_project(project_id)
    if not proj:
        raise RuntimeError(f"project {project_id} is gone")

    ws = _prepare_workspace(aid)
    token = await A.token_for(aid)
    if not token:
        raise RuntimeError("assistant has no control-plane token")

    remote = ""
    if A.has(assistant, "read_repo"):
        remote = await _git_remote_for(proj, str(proj.get("user_id") or ""))

    # THE SHARED BRAIN (phase 32). Every assistant on a project gets the SAME
    # `workspace-api-key`, which is what makes the tracker shared: the Developer files a bug,
    # the Tester sees it, the Product Owner comments on it. `ensure_key` mints one on first
    # ask, so projects that existed before phase 32 are never stranded without one.
    #
    # It arrives HERE, per beat, in the container environment — it is deliberately NOT baked
    # into the runtime image. An image is pulled, inspected, copied and shared; a credential
    # inside one is a credential in everyone's hands. `docker run --rm --entrypoint sh
    # mikeos-assistant-runtime -c env` must show nothing.
    ws_key = ""
    try:
        ws_key = await W.ensure_key(project_id)
    except Exception as e:  # noqa: BLE001 — a tracker outage must not stop the assistant
        logger.warning("no workspace key for %s (%s) — this beat runs without `ws`",
                       project_id, e)

    name = f"asst-{aid}-{beat_id}"
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "--network", NETWORK,
        # --- hardening -----------------------------------------------------
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", f"{RUN_UID}:{RUN_UID}",
        "--memory", MEM_LIMIT, "--memory-swap", MEM_LIMIT,
        "--cpus", str(CPU_LIMIT), "--pids-limit", str(PIDS_LIMIT),
        # /tmp is the ONLY writable place outside the checkout: Pi's models.json, the
        # grounding file, and node's scratch all live there. Still a tmpfs on a read-only
        # rootfs — nothing an agent writes outside its own repo survives the beat.
        "--read-only", "--tmpfs", "/tmp:rw,size=512m,mode=1777",
        # --- the one writable thing it has: its own checkout ----------------
        "-v", f"{ws}:/workspace",
        # --- identity + config ---------------------------------------------
        "-e", f"CONTROL_URL={CONTROL_URL}",
        "-e", f"ASSISTANT_TOKEN={token}",
        "-e", f"BEAT_ID={beat_id}",
        "-e", f"ASSISTANT_ID={aid}",
        "-e", f"PROJECT_ID={project_id}",
        "-e", f"TRIGGER_KIND={trigger_kind}",
        "-e", f"CAPABILITIES={','.join(assistant.get('capabilities') or [])}",
        "-e", "HOME=/tmp",
        "-e", "GIT_TERMINAL_PROMPT=0",
    ]
    if remote:
        cmd += ["-e", f"GIT_REMOTE={remote}"]
    if ws_key:
        cmd += ["-e", f"WORKSPACE_API_KEY={ws_key}"]
    cmd.append(IMAGE)

    try:
        return await _run(cmd, timeout=BEAT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        # A hung beat is killed on a hard timeout and recorded failed — never left to run
        # forever holding the single beat slot.
        logger.warning("beat %s for assistant %s timed out after %ss — killing %s",
                       beat_id, aid, BEAT_TIMEOUT_SEC, name)
        await _run(["docker", "rm", "-f", name], timeout=60)
        raise


def _redact(text: str) -> str:
    """Never let a token reach a log or a beat record."""
    text = re.sub(r"asst_[A-Za-z0-9_\-]{8,}", "asst_***", text or "")
    # …and the project's shared workspace key, which the beat's `ws` tool carries (phase 32).
    text = re.sub(r"wsk_[A-Za-z0-9_\-]{8,}", "wsk_***", text)
    return re.sub(r"://[^/@\s]+:[^/@\s]+@", "://***:***@", text)


async def open_beat(assistant: dict, trigger_kind: str, user_ask: str = "") -> int:
    """Open the beat row. Split out from `execute_beat` so `POST /beat` can hand the caller a
    beat id immediately and let the UI show a running beat while the container works."""
    return await A.start_beat(int(assistant["id"]), str(assistant["project_id"]),
                              trigger_kind, user_ask)


async def execute_beat(assistant: dict, beat_id: int, trigger_kind: str) -> int:
    """Run the container for an already-claimed assistant and always release it.

    Every terminal state is written by SOMEBODY: the container writes the interesting one
    (thought + actions + cost) through `/api/assistant/beat`; this function writes the boring
    one (crashed / timed out) if the container never got that far.
    """
    aid = int(assistant["id"])
    started = time.monotonic()
    async with _beat_slot:
        try:
            rc, out = await launch_beat(assistant, beat_id, trigger_kind=trigger_kind)
            if rc != 0:
                # The beat program records its own failures; this catches the ones where it
                # could not (image start failure, OOM kill, non-zero without a report).
                await _finish_if_running(
                    beat_id, "failed",
                    log=f"beat container exited {rc}\n{_redact(out)[-4000:]}",
                    duration_ms=int((time.monotonic() - started) * 1000))
            else:
                await _finish_if_running(
                    beat_id, "done", log=_redact(out)[-4000:],
                    duration_ms=int((time.monotonic() - started) * 1000))
        except asyncio.TimeoutError:
            await _finish_if_running(
                beat_id, "failed",
                log=f"beat exceeded the {BEAT_TIMEOUT_SEC}s hard timeout and was killed",
                duration_ms=int((time.monotonic() - started) * 1000))
        except Exception as e:  # noqa: BLE001 — one bad beat must never kill the scheduler
            logger.exception("beat %s for assistant %s failed to run", beat_id, aid)
            await _finish_if_running(
                beat_id, "failed", log=_redact(str(e))[:4000],
                duration_ms=int((time.monotonic() - started) * 1000))
        finally:
            fresh = await A.get(aid)
            mins = A.clamp_interval((fresh or assistant).get("interval_minutes"), 60)
            await A.release(aid, schedule_next_minutes=mins)
    return beat_id


async def _finish_if_running(beat_id: int, status: str, *, log: str = "",
                             duration_ms: int = 0) -> None:
    """Close a beat only if the container did not already report a richer record."""
    from server import browser_proxy               # local import: avoid an import cycle
    from server.db import pool
    # Whatever happens to the beat row, a dead container's browser sessions are released.
    # This is the path a CRASHED beat takes — it never reaches `/api/assistant/beat`, so
    # without this the OOM-kill case is exactly the one that leaks into the shared pool.
    try:
        await browser_proxy.close_beat_sessions(beat_id)
    except Exception:  # noqa: BLE001
        logger.debug("browser cleanup failed for beat %s", beat_id, exc_info=True)
    cur = await pool().fetchval(
        "SELECT status FROM builderapps.assistant_beats WHERE id=$1", beat_id)
    if cur != "running":
        return
    await A.finish_beat(beat_id, status=status, log=log, duration_ms=duration_ms)


# ---------------------------------------------------------------------------
# scheduler — one loop for every assistant, NOT a timer per assistant
# ---------------------------------------------------------------------------
async def _scheduler_loop() -> None:
    while not _shutting_down:
        try:
            await asyncio.sleep(SCHED_INTERVAL_SEC)
            # The WAKE pass runs FIRST and on the same loop. A message from a colleague is
            # the most time-sensitive reason to beat — somebody is blocked on the answer —
            # and running it on its own timer would be a second scheduler racing the first
            # for the single beat slot.
            await wake_tick()
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the scheduler must never die
            logger.exception("assistant scheduler iteration failed")


async def _budget_blocks(project_id: str) -> Optional[dict]:
    """THE $10/DAY HARD STOP. Returns the budget status when this project may not beat.

    Checked before the claim, not after: a claimed assistant with no beat is an assistant
    that looks busy and is not. And checked for EVERY trigger — schedule, DM wake and the
    owner's own button — because a ceiling with a manual override is not a ceiling.
    """
    try:
        st = await budget.allows_beat(project_id)
    except Exception:  # noqa: BLE001 — never let the accounting query stop the platform
        logger.debug("budget check failed for %s", project_id, exc_info=True)
        return None
    if not st.stopped:
        return None
    try:
        if await budget.announce_once(project_id, st):
            ws_hub.publish(project_id, {"type": "budget", "budget": st.as_dict()})
    except Exception:  # noqa: BLE001
        logger.debug("budget announce failed for %s", project_id, exc_info=True)
    return st.as_dict()


async def wake_tick() -> int:
    """One WAKE pass: start a beat for every assistant that has been sent a message.

    This function is the delivery mechanism, and it does not know what a browser is. A DM was
    written to Postgres and a flag was raised; here the flag becomes a beat. Everything it
    relies on — `claim`, `release`, the boot sweep — is the machinery the scheduled path
    already uses, so a redeploy mid-wake is recovered by code that was already being
    exercised every hour rather than by a second, untested path.
    """
    if _shutting_down or not ENABLED:
        return 0
    try:
        woken = await M.wake_due()
    except Exception as e:  # noqa: BLE001
        logger.warning("assistant wake query failed: %s", e)
        return 0
    n = 0
    for w in woken:
        project_id = str(w["project_id"])
        if await _budget_blocks(project_id):
            # Leave the flag raised. The messages stay unread and the wake happens after
            # midnight — held, not dropped, which is what the owner is told.
            continue
        claimed = await A.claim(int(w["id"]), runner.INSTANCE_ID, any_status=True)
        if not claimed:
            continue                     # it is already beating; it will read its inbox there
        try:
            # Clear the flag only once we hold the claim, so a crash before this point simply
            # wakes again, and coalescing resumes from here: a DM arriving DURING this beat
            # raises the flag afresh and earns its own beat afterwards.
            await M.clear_wake(int(w["id"]))
            beat_id = await open_beat(claimed, "dm")
            msgs = await M.claim_for_beat(int(w["id"]), beat_id)
            if not msgs:
                # The flag was raised but the inbox is empty — another beat already read it.
                # Close the empty beat rather than spending a reasoning round on nothing.
                await _finish_if_running(beat_id, "skipped",
                                         log="woken with an empty inbox — already read")
                await A.release(int(w["id"]))
                continue
            await A.set_beat_ask(beat_id, await M.wake_task(project_id, msgs))
            await M.publish_feed(project_id)
            await execute_beat(claimed, beat_id, "dm")
            await M.mark_read([int(m["id"]) for m in msgs], beat_id)
            await M.publish_feed(project_id)
            n += 1
        except Exception:  # noqa: BLE001
            logger.exception("assistant %s wake beat failed", w.get("id"))
            await A.release(int(w["id"]))
    return n


async def tick() -> int:
    """One scheduling pass: claim every due assistant and beat them, one at a time."""
    if _shutting_down or not ENABLED:
        return 0
    try:
        due = await A.due_now()
    except Exception as e:  # noqa: BLE001
        logger.warning("assistant due query failed: %s", e)
        return 0
    n = 0
    for a in due:
        if await _budget_blocks(str(a["project_id"])):
            # Push the next attempt out rather than spinning on a due row every 60s: the
            # scheduler would otherwise re-announce and re-check the same stopped project
            # once a minute until midnight.
            await A.release(int(a["id"]), schedule_next_minutes=30)
            continue
        claimed = await A.claim(int(a["id"]), runner.INSTANCE_ID)
        if not claimed:
            continue                     # another scheduler won the race
        try:
            beat_id = await open_beat(claimed, "schedule")
            await execute_beat(claimed, beat_id, "schedule")
            n += 1
        except Exception:  # noqa: BLE001
            logger.exception("assistant %s beat failed", a.get("id"))
    return n


async def kick(assistant: dict, user_ask: str = "") -> Optional[tuple[dict, int]]:
    """`POST /beat` — claim + open a beat row and return (assistant, beat_id) NOW; the caller
    runs `execute_beat` in the background so the HTTP request does not block for a minute.
    Returns None when a beat is already in flight, so two impatient clicks can never
    double-run an assistant.

    `user_ask` is set when a human addressed this assistant directly from the composer
    (`@Developer add a search box`). It is stored on the beat row, so the reasoning call
    reads it from the database rather than from anything the container asserts.

    Raises `BudgetStop` when the project has spent its day. Deliberately an exception on THIS
    path and a `continue` on the scheduler's: a human who pressed a button is owed an answer
    saying why nothing happened, whereas a scheduler has nobody to tell.
    """
    stop = await _budget_blocks(str(assistant["project_id"]))
    if stop:
        raise BudgetStop(stop)
    claimed = await A.claim(int(assistant["id"]), runner.INSTANCE_ID, any_status=True)
    if not claimed:
        return None
    return claimed, await open_beat(claimed, "ask" if user_ask else "manual", user_ask)


class BudgetStop(RuntimeError):
    """The project has spent its daily budget; no beat was started."""

    def __init__(self, status: dict):
        super().__init__("daily budget reached")
        self.status = status


async def sweep_on_boot() -> int:
    """A control-plane redeploy kills every beat container's supervisor. Release the claims
    it left behind and mark the orphaned beats failed, so no assistant is stranded."""
    released = await A.sweep_orphaned_claims(runner.INSTANCE_ID)
    failed = await A.sweep_running_beats("control plane restarted mid-beat")
    # THE SAME CONTRACT FOR MAIL. A beat that died after claiming its inbox left those rows
    # `delivered` but never `read`, and the wake flag cleared — so without this the message
    # is in the table, nobody is coming for it, and the sender is waiting for a reply that
    # will never arrive. Re-raising the flag is safe precisely because raising it is
    # idempotent (that is also what makes duplicate wakes coalesce).
    rewoken = await M.resweep_unread()
    if released or failed or rewoken:
        logger.warning("assistant boot sweep: released %d claim(s), failed %d orphan beat(s), "
                       "re-woke %d assistant(s) with unread mail", released, failed, rewoken)
    return released


def start_scheduler() -> None:
    global _sched_task
    if not ENABLED:
        logger.info("assistant scheduler disabled (ASSISTANTS_ENABLED=0)")
        return
    if _sched_task is None or _sched_task.done():
        _sched_task = asyncio.create_task(_scheduler_loop())


async def shutdown() -> None:
    global _shutting_down, _sched_task
    _shutting_down = True
    if _sched_task:
        _sched_task.cancel()
        _sched_task = None


# ===========================================================================
# The act surface — what a beat container may ask the control plane to do.
# EVERY function here calls A.require() first. That call is the enforcement.
# ===========================================================================
async def perceive(assistant: dict, *, include_soul: bool = False) -> dict:
    """A compact, honest snapshot of the project for the reasoning prompt.

    Deliberately small: an assistant that is handed 40k tokens of context every 15 minutes
    is an expense, not a colleague. Cost-shaped facts are only included when the assistant
    was granted `read_costs`.
    """
    project_id = str(assistant["project_id"])
    proj = await store.get_project(project_id) or {}
    latest = await store.steps_for_latest_run(project_id)
    steps = latest.get("steps") or []
    ctx: dict[str, Any] = {
        "project": {
            "id": project_id,
            "title": proj.get("title") or "",
            "brief": (proj.get("prompt") or "")[:1200],
            "status": proj.get("status") or "",
            "url": f"https://{project_id}.{os.environ.get('SITES_BASE', 'builderapps.osmike.com')}/",
        },
        "latest_run": {
            "status": latest.get("status"),
            "summary": (latest.get("summary") or "")[:600],
            "error": (latest.get("error") or "")[:600],
            "steps": [{"name": s.get("name"), "status": s.get("status")} for s in steps][-24:],
        },
        "deployments": [
            {"status": d.get("status"), "health": (d.get("health") or "")[:200],
             "started_at": str(d.get("started_at") or "")}
            for d in (await store.list_deployments(project_id, 3))
        ],
    }

    # THE SHARED BOARD (phase 32). Every assistant sees the project's live workspace on every
    # beat, deterministically — it is not a tool the model has to remember to reach for. This
    # is what stops two assistants rebuilding the same thing and what lets one pick up a bug
    # another filed.
    #
    # POSITION MATTERS. `reason()` serializes this whole dict and hard-slices it at 14,000
    # chars, and json.dumps preserves insertion order — so anything added at the END is the
    # first thing thrown away on exactly the busy projects this block exists for. It goes
    # near the front, and it is kept deliberately small (20 live items, titles clipped) so it
    # is cheap enough to always carry.
    #
    # `open_only` + `newest_first` are load-bearing too: fetching the first N rows by id and
    # THEN dropping the closed ones spends the whole window on history, so a project with a
    # finished backlog would hand the agents an empty board and nothing would fail.
    try:
        live_items = await W.list_items(project_id, limit=20, open_only=True,
                                        newest_first=True)
        ctx["workspace"] = {
            "counts": await W.counts(project_id),
            "open_items": [{"id": i["id"], "kind": i["kind"], "status": i["status"],
                            "title": i["title"][:120],
                            "by": i.get("created_by_name") or i.get("created_by")}
                           for i in live_items],
        }
    except Exception as e:  # noqa: BLE001 — perception must never fail a beat
        logger.info("workspace board unavailable for %s: %s", project_id, e)

    # WHO ELSE IS HERE (phase 33). An assistant cannot message a colleague it does not know
    # exists, so the roster is perception, not a tool call — the same reasoning as the board
    # above, and for the same position reason it sits next to it rather than at the end.
    # `you_are` is included because a model given a list of five names and no marker for
    # itself will eventually address itself; `send` refuses that, but a refused action is a
    # wasted beat and this costs twelve tokens.
    try:
        ctx["colleagues"] = await M.roster(project_id, exclude_id=int(assistant["id"]))
        ctx["you_are"] = {"id": int(assistant["id"]),
                          "name": assistant.get("name") or "",
                          "role": assistant.get("role") or ""}
    except Exception as e:  # noqa: BLE001
        logger.info("roster unavailable for %s: %s", project_id, e)

    try:
        thread = await store.get_raw_thread(project_id)
        ctx["recent_thread"] = [
            {"role": str(t.get("role") or ""), "text": str(t.get("text") or "")[:400]}
            for t in thread[-8:]]
    except Exception:  # noqa: BLE001
        ctx["recent_thread"] = []
    if A.has(assistant, "read_costs"):
        try:
            ctx["usage"] = await store.usage_for_project(project_id)
        except Exception:  # noqa: BLE001
            ctx["usage"] = {}
    try:
        # Its own memory — but NEVER the beat it is executing right now. Handing an agent a
        # row that says `running` with no thought makes it reason about its own in-flight
        # self ("my last beat has no results yet, so let me redo it"), which is exactly the
        # kind of confused, duplicated work a memory is supposed to prevent.
        prior = [b for b in await A.list_beats(int(assistant["id"]), 6)
                 if b.get("status") != "running"][:3]
        ctx["my_recent_beats"] = [
            {"ts": str(b.get("ts") or ""), "status": b.get("status"),
             "thought": (b.get("thought") or "")[:400],
             "actions": [a.get("type") for a in (b.get("actions") or [])]}
            for b in prior]
    except Exception:  # noqa: BLE001
        ctx["my_recent_beats"] = []
    # The platform's own contracts, sent to the container rather than baked into its image:
    # a beat is a DIFFERENT runtime (it cannot import server.harness), and these rules change
    # when the platform changes, not when that image is next rebuilt. Same words the build
    # pipeline's codegen prompts carry, so an assistant cannot unknowingly "harden" away
    # something the pipeline is contractually required to keep.
    ctx["platform_rules"] = codegen.PLATFORM_CONTRACTS
    if include_soul:
        # Only for the container's GET /context, so it can mirror the SOUL into the repo at
        # `docs/assistants/<role>.SOUL.md` — the SOUL should live in git next to the app it
        # serves. NEVER included in the reasoning context: the SOUL is already the system
        # prompt, and sending it twice is pure waste.
        ctx["assistant"] = {
            "id": int(assistant["id"]),
            "role": assistant.get("role") or "",
            "name": assistant.get("name") or "",
            "capabilities": assistant.get("capabilities") or [],
            "soul_path": f"docs/assistants/{A.slug(assistant.get('role') or 'assistant')}.SOUL.md",
            "soul_md": assistant.get("soul_md") or "",
        }
    return ctx


_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "text": {"type": "string"},
                    "task": {"type": "string"},
                    "message": {"type": "string"},
                    "request": {"type": "string"},
                    "capability": {"type": "string"},
                    # the shared workspace (phase 32)
                    "kind": {"type": "string"},
                    "title": {"type": "string"},
                    "status": {"type": "string"},
                    "item_id": {"type": "integer"},
                    # messaging a colleague (phase 33)
                    "to": {"type": "string"},
                },
                "required": ["type"],
            },
        },
        "done": {"type": "boolean"},
    },
    "required": ["thought", "actions", "done"],
}


def _action_menu(assistant: dict) -> str:
    """Only the actions this assistant was actually granted are even described to it. A
    capability it does not hold is not 'refused later' — it is never offered."""
    lines = []
    if A.has(assistant, "comment"):
        lines.append('- {"type":"comment","text":"..."} — post a finding or proposal into the '
                     "project thread the owner reads.")
    if A.has(assistant, "run_qa"):
        lines.append('- {"type":"run_qa"} — load the LIVE app in a REAL BROWSER, click '
                     "through it, and report the rendered page, the JS console errors and "
                     "the failed requests. This sees what curl and /health cannot: an empty "
                     "list, a dead button, a blank screen. Use it before concluding anything "
                     "about whether a page works.")
    if A.has(assistant, "edit_code"):
        # ONE coding action, not a file-by-file protocol. The editing is done by Pi, an
        # open-source coding agent running in this assistant's own container with the
        # checkout in front of it; it reads, greps and edits on its own. What is wanted here
        # is the BRIEF for that agent, so `task` should read like a ticket, not a diff.
        lines.append(
            '- {"type":"code","task":"<what to build, as a precise engineering brief>",'
            '"message":"<git commit message>"} — hand ONE task to the coding agent that '
            "runs in your container with the repo checked out. It will read the code, make "
            "the change"
            + (", commit and push it" if A.has(assistant, "commit_push") else "")
            + (", and the control plane will then build it and health-gate it; if it fails "
               "the gate it is rolled back automatically."
               if A.has(assistant, "request_deploy") else ".")
            + " Describe the OUTCOME and the constraints; do not write the code here.")
    if A.has(assistant, "request_deploy"):
        lines.append('- {"type":"request_deploy","request":"why"} — ship whatever is already '
                     "committed on HEAD: build + health gate, rolled back if it goes red. "
                     "You never touch Docker. Only useful if a commit is already pushed "
                     "that has not been deployed.")
    # THE SHARED WORKSPACE — offered to EVERY assistant, with no capability gate.
    #
    # Deliberate: writing to your own project's work-tracker is not a privilege, it is how
    # you exist to your colleagues and to the owner. It touches no code, ships nothing, and
    # cannot leave the project. Gating it behind a capability would mean the read-only
    # assistants — exactly the ones whose entire output IS findings and knowledge — could
    # observe carefully and then have nowhere to put it.
    lines.append(
        '- {"type":"workspace_add","kind":"bug|feature|task|testcase|doc|kb","title":"...",'
        '"text":"the details someone else could act on"} — put something on the project\'s '
        "shared board, where the pipeline, the other assistants and the owner all read it. "
        "This is the only thing you produce that outlives this beat. `kind` is free text.")
    lines.append(
        '- {"type":"workspace_update","item_id":42,"status":"in_progress|done|blocked",'
        '"text":"a comment explaining the change"} — move an item on the board (its id is in '
        "the `workspace` block of your context) and/or comment on it. Only mark something "
        "`done` if you actually verified it.")
    # MESSAGING A COLLEAGUE (phase 33). Ungated, like the board: talking to the people you
    # work with is not a privilege. Bounded instead of forbidden — see `act_message`.
    #
    # The description leads with the cost and with "no reply needed", because the failure
    # mode here is not an agent that refuses to message, it is an agent that is unfailingly
    # polite: every message it receives gets a courteous acknowledgement, each of which wakes
    # a container and costs real money to say "thanks".
    lines.append(
        '- {"type":"message","to":"<colleague name or role>","text":"...","item_id":42} — '
        "send a direct message to another assistant on this project (they are listed in the "
        "`colleagues` block of your context). It WAKES them: a whole beat starts to read it, "
        "which costs the project real money. `item_id` is optional and is the point of the "
        "action — reference a board item and they receive the entire report, not your "
        "summary of it. Message someone when you need THEM to do something you cannot. Do "
        "not message to acknowledge, to thank, or to confirm you have read something: "
        "silence is the correct answer to good news.\n"
        "  You CAN file an item and message someone about it in the same beat — emit "
        '{"type":"workspace_add",…} first and then {"type":"message",…}. You will not know '
        "the new item's id when you write the message; put your best guess in `item_id` and "
        "the platform attaches the item you just filed.")
    lines.append('- {"type":"none"} — nothing is worth doing this beat. Preferred over noise.')
    return "\n".join(lines)


async def reason(assistant: dict, context: dict, workspace_report: str = "",
                 docs: str = "", ask: str = "", trigger_kind: str = "") -> dict:
    """ONE LLM call: docs + SOUL + role + context -> {thought, actions[], done}.

    The ORDER of the prompt is deliberate and is Mike's: **the project's own documents come
    first**, then the SOUL, then the current state, then the decision. An assistant with
    judgment but no knowledge of what the product is *for* optimises the wrong thing, so the
    vision/mission is the grounding and the persona sits on top of it. The docs are loaded
    deterministically by the beat program — never left to the model to remember to go and
    read them.

    Cost is attributed to the project (step `assistant:<id>`), so an assistant shows up in
    the project's Usage tab like everything else — an assistant that quietly burns money is
    the failure mode this accounting exists to make impossible.
    """
    project_id = str(assistant["project_id"])
    soul = (assistant.get("soul_md") or "").strip()[:A.MAX_SOUL_CHARS]
    role = assistant.get("role") or "Assistant"
    caps = assistant.get("capabilities") or []
    max_actions = MAX_ACTIONS_DM if trigger_kind == "dm" else MAX_ACTIONS

    system = (
        # 1. THE PRODUCT — what this thing is for, from the project's own docs.
        ("# The product you look after\n\nThese are this project's own documents. They are "
         "the ground truth about what it is for and who it is for; everything you decide is "
         "judged against them.\n\n" + docs[:20000] + "\n\n---\n\n" if docs else "")
        # 2. THE PERSONA — who you are, on top of that grounding.
        + f"{soul}\n\n"
        "---\n"
        f"You are an autonomous assistant attached to ONE software project. Your role is "
        f"'{role}'. You run on a heartbeat; this is one beat. You are not chatting with a "
        "human — you either do something useful or you deliberately stay quiet.\n\n"
        f"Capabilities you have been GRANTED: {', '.join(caps) or '(none)'}. Anything not on "
        "that list is refused by the control plane, so do not attempt it.\n\n"
        + ("You do NOT write code in this reply. You decide WHAT should be done and hand it "
           "to a coding agent that is already running in your container with the repository "
           "checked out — it reads and edits the files itself. Your job is to pick the ONE "
           "most valuable change and brief it precisely.\n\n"
           if A.has(assistant, "edit_code") else "")
        # The single most important line for an assistant that has one: you can SEE the page.
        # Without this, a model reasons from deploy records and invents a cause that fits.
        + ("**You have a real browser.** Use it to SEE the page before you claim anything "
           "about whether it works — an HTTP 200 and a green health check prove a process "
           "answered, not that a user can use it. A previous assistant here diagnosed a "
           "broken site as a certificate problem at the platform ingress when the real cause "
           "was a header it had added itself; it had no browser, so it guessed. Your coding "
           "agent has the `mikeweb` command for this mid-task, and the page is loaded in a "
           "browser automatically after anything you ship.\n\n"
           if A.has(assistant, "run_qa") else "")
        # The contracts belong here too, not only in the coding agent's grounding: the
        # decision to "add clickjacking protection" is taken HERE, and a brief that asks for
        # the wrong header produces a correct-looking change that breaks the owner's preview.
        + "---\n" + codegen.PLATFORM_CONTRACTS + "\n\n---\n\n"
        + "Available actions:\n" + _action_menu(assistant) + "\n\n"
        "Rules:\n"
        "- Reply with JSON only: {\"thought\": str, \"actions\": [...], \"done\": bool}.\n"
        "- `thought` is one short paragraph a busy human would actually read: what you "
        "looked at and what you concluded. No preamble.\n"
        f"- At most {max_actions} actions. Emit {{\"type\":\"none\"}} when the honest "
        "answer is that "
        "nothing changed and nothing is worth saying.\n"
        "- Never claim to have done something you did not do.\n"
    )
    user = ("PROJECT CONTEXT (JSON):\n" + json.dumps(context, default=str)[:14000]
            + ("\n\nWORKSPACE (what your checkout looks like right now):\n"
               + workspace_report[:12000] if workspace_report else ""))
    if ask and trigger_kind == "dm":
        # A COLLEAGUE, not a customer. Same position in the prompt as a human ask — last and
        # unmistakable — but framed differently on purpose, because the two want opposite
        # default behaviour at the end. A human who asks deserves an answer; a colleague who
        # says "fixed, please retest" deserves a RETEST, and a reply saying "great, thanks"
        # is a beat, a container and up to $1.59 spent on politeness. So the closing
        # instruction here is explicitly permission to say nothing.
        user += (
            "\n\n=== A COLLEAGUE ON THIS PROJECT HAS MESSAGED YOU ===\n"
            f"{ask[:A.MAX_ASK_CHARS]}\n"
            "=== end of the message(s) ===\n\n"
            "You were woken by that message: this beat exists because of it, so it decides "
            "what you do now. DO THE WORK IT ASKS FOR, using the capabilities you hold — if "
            "it points at a workspace item, the whole item is quoted above, so act on it "
            "rather than asking for more detail.\n\n"
            "**Replying is optional and is usually wrong.** Every message you send wakes a "
            "whole container and costs the project real money. Reply ONLY if the sender "
            "cannot continue without something from you — an answer to a question, or the "
            "news that the thing they are waiting on is ready. Do NOT reply to acknowledge, "
            "to thank, to confirm receipt, or to say you have started. Recording what you "
            "did on the workspace board is how you report; it costs nobody a beat, and the "
            "sender sees it there. If nothing needs saying, say nothing.\n\n"
            "But if they asked to be TOLD when something is done — 'message me when it is "
            "deployed so I can retest' — then telling them IS the work, and you have an "
            "extra action slot this beat precisely so you can do the job AND close the loop. "
            "Do not leave a colleague waiting on a promise you kept but never mentioned.")
    elif ask:
        # LAST, and unmistakable. The grounding order is docs -> SOUL -> state -> the ask, so
        # an addressed assistant still knows what the product is for before it acts. But a
        # human who typed `@you <something>` is not asking for the agent's opinion on what
        # would be most valuable this beat — this beat has already been decided for it.
        user += (
            "\n\n=== A HUMAN IS ADDRESSING YOU DIRECTLY ===\n"
            f"{ask[:A.MAX_ASK_CHARS]}\n"
            "=== end of the request ===\n\n"
            "This overrides your own judgement about what to do this beat: do THIS, using "
            "the capabilities you hold. If it is a change to the app and you can edit code, "
            "make the change. If you cannot do what was asked — wrong capability, or the "
            "request is unclear or unsafe — say so plainly in a comment rather than doing "
            "something else instead.")

    usage.set_context(project_id, None, f"assistant:{assistant.get('id')}")
    try:
        with usage.capture() as recs:
            raw = await gpu.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                schema=_ACTION_SCHEMA, temperature=0.4, num_predict=1600, timeout=180.0,
                max_retries=2)
    finally:
        usage.clear_context()

    tokens = sum(int(r.get("prompt_tokens") or 0) + int(r.get("completion_tokens") or 0)
                 for r in recs)
    cost = sum(float(r.get("cost_usd") or 0.0) for r in recs)

    parsed = _parse_reply(raw, max_actions)
    parsed["tokens"] = tokens
    parsed["cost_usd"] = round(cost, 6)
    # Told to the CONTAINER, not just applied here: it is the container that walks the action
    # list, so a cap the control plane knows about and the container does not is a cap that
    # silently drops the third action after the model was invited to plan it.
    parsed["max_actions"] = max_actions
    return parsed


# How many actions ONE beat may take. Two, so a beat is a decision and not a shopping list —
# except when a colleague woke it, where the third slot is not scope creep but the ANSWER.
#
# Found the hard way on the first real hand-off: the Tester asked the Developer to fix #21 and
# to say when it was live. The Developer fixed it, shipped it, moved the board item — and had
# no slot left to reply, so the Tester was never told and the loop it was explicitly asked to
# close stayed open. Making it choose between doing the work and answering is the wrong
# trade; the reply is what makes the hand-off a hand-off.
MAX_ACTIONS = 2
MAX_ACTIONS_DM = 3


def _parse_reply(raw: str, max_actions: int = MAX_ACTIONS) -> dict:
    """Tolerant parse — a model that wraps its JSON in prose must not fail the beat."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    data: Any = None
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                data = None
    if not isinstance(data, dict):
        # Never throw away the model's words: an unparseable reply still becomes the thought.
        return {"thought": text[:4000], "actions": [], "done": True, "parse_error": True}
    acts = data.get("actions")
    if not isinstance(acts, list):
        acts = []
    clean = [a for a in acts if isinstance(a, dict) and a.get("type")][:max(1, max_actions)]
    return {"thought": str(data.get("thought") or "")[:8000], "actions": clean,
            "done": bool(data.get("done", True))}


# ---- individual acts ------------------------------------------------------
async def act_comment(assistant: dict, text: str) -> dict:
    A.require(assistant, "comment")
    body = (text or "").strip()
    if not body:
        return {"ok": False, "detail": "empty comment"}
    label = assistant.get("name") or assistant.get("role") or "Assistant"
    await store.append_message(
        str(assistant["project_id"]), "assistant",
        f"**{label}** ({assistant.get('role') or 'assistant'}) — {body}"[:4000],
        meta={"assistant_id": int(assistant["id"]), "kind": "assistant_note"})
    return {"ok": True, "posted": True}


async def act_run_qa(assistant: dict) -> dict:
    """Drive the LIVE app through chrome-pool and report what actually happened.

    Deliberately `chrome.qa_run` (navigate + exercise + collect console/network errors) and
    NOT the pipeline's `runtime_qa.run_qa`: that one includes the AUTOFIX loop, which writes
    into `/opt/builderapps/workspaces/<id>` — the pipeline's tree. A Tester assistant running
    on a heartbeat must never race a build over that checkout, and `run_qa` is supposed to be
    a read-only capability. Observing is the assistant's job; fixing goes through
    `request_deploy`.
    """
    A.require(assistant, "run_qa")
    project_id = str(assistant["project_id"])
    url = f"https://{project_id}.{os.environ.get('SITES_BASE', 'builderapps.osmike.com')}/"
    try:
        qa = await chrome.qa_run(url, exercise=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"QA failed: {e}"[:400]}
    errors = (qa or {}).get("errors") or []
    network = (qa or {}).get("network") or []
    text = str((qa or {}).get("text") or "")
    # never-trust: chrome-pool degrades to empty lists on its own failure, so `ok` matters
    return {"ok": True, "reached": bool((qa or {}).get("ok")), "url": url,
            "console_errors": [str(e)[:300] for e in errors[:10]],
            "failed_requests": [str(n)[:300] for n in network[:10]],
            # The rendered page, so the assistant can QUOTE what a visitor sees instead of
            # only reporting the absence of errors. An empty page with a clean console is
            # still a broken page, and this is the only field that shows it.
            "rendered_text": text[:4000],
            "rendered_chars": len(text),
            "blank": bool((qa or {}).get("ok")) and len(text.strip()) < 40,
            "clean": bool((qa or {}).get("ok")) and not errors and not network
            and len(text.strip()) >= 40}


async def act_request_deploy(assistant: dict, request_text: str,
                             beat_id: Optional[int] = None) -> dict:
    """THE SAFETY BOUNDARY IN CODE — and it is a SHIP, not the build pipeline.

    The assistant does not deploy. It asks, and the control plane ships the repo's current
    HEAD: compose normalizer -> docker build -> health gate -> roll back to the last good
    commit if the gate goes red (`server.shipper`). The assistant container has no Docker
    socket and never will.

    Deliberately NOT `pipeline.run_update`. That pipeline re-plans a minimal diff with its
    own LLM pass; running it here would have a second AI second-guess — and overwrite — the
    change the assistant's own coding agent just made and pushed. `request_deploy` means one
    thing only: *"I pushed a commit; ship the current HEAD."*
    """
    A.require(assistant, "request_deploy")
    from server import shipper                    # local import: avoid an import cycle
    project_id = str(assistant["project_id"])
    proj = await store.get_project(project_id)
    if not proj:
        return {"ok": False, "detail": "project is gone"}
    if proj.get("status") in ("creating", "building", "deploying"):
        # NO DEPLOY STORM. A ship never starts on top of a run that is already in flight —
        # the caller is told to come back, it does not queue up behind it.
        return {"ok": False, "busy": True,
                "detail": f"a run is already in flight for this project "
                          f"(status={proj.get('status')}) — not starting a second deploy"}
    user_id = str(proj.get("user_id") or "")
    reason = (request_text or "").strip()[:2000] or "ship the pushed commit"
    aid = int(assistant["id"])
    run_id = await store.create_run(project_id, "deploy", reason,
                                    total_steps=len(shipper.STEP_NAMES),
                                    assistant_id=aid, beat_id=beat_id)
    await store.set_project_status(project_id, "deploying")
    emit = runner.emitter(run_id)
    await runner.start(run_id, project_id,
                       lambda: shipper.run_ship(project_id, run_id, user_id, None, reason,
                                                emit, assistant_id=aid, beat_id=beat_id))
    return {"ok": True, "run_id": run_id, "detail": "shipping HEAD (build + health gate)"}


async def deploy_status(assistant: dict, run_id: int) -> dict:
    """Where has the ship this beat started got to? The beat container polls this so its
    record carries the REAL outcome — a beat that says "done" while the health gate was
    still running would be exactly the "never trust a 200" failure this codebase keeps
    relearning."""
    A.require(assistant, "request_deploy")
    row = await store.get_run(run_id)
    if not row or str(row.get("project_id")) != str(assistant["project_id"]):
        return {"ok": False, "detail": "no such run for this project"}
    steps = await store.get_run_with_steps(run_id) or {}
    return {
        "ok": True,
        "run_id": run_id,
        "status": row.get("status"),                       # running | done | failed
        "finished": row.get("status") in ("done", "failed"),
        "error": _redact(str(row.get("error") or ""))[:1200],
        "summary": str(row.get("summary") or "")[:600],
        "steps": [{"name": s.get("name"), "status": s.get("status"),
                   "detail": _redact(str(s.get("log") or ""))[:400]}
                  for s in (steps.get("steps") or [])],
    }


async def act_costs(assistant: dict) -> dict:
    A.require(assistant, "read_costs")
    return await store.usage_for_project(str(assistant["project_id"]))


async def act_check(assistant: dict, capability: str) -> dict:
    """Ask the CONTROL PLANE whether a capability is granted, before doing the local work.

    `edit_code` and `commit_push` necessarily execute inside the container — that is where
    the checkout and git are. The container knows its own capability list, but that list is
    a convenience, not a boundary: a tampered container could simply ignore it. So the
    container asks here first, and this answer is the authority.
    """
    A.require(assistant, capability)
    return {"ok": True, "granted": capability}


def _ws_actor(assistant: dict) -> W.Actor:
    """Who this assistant is on the board. Resolved from the ROW, never from anything the
    container sent — an assistant cannot file work under a colleague's name."""
    return W.Actor.assistant(assistant["id"],
                             assistant.get("name") or assistant.get("role") or "assistant")


async def act_workspace_add(assistant: dict, action: dict) -> dict:
    """File a new item on the project's shared board (phase 32). No capability gate — see
    `_action_menu`."""
    project_id = str(assistant["project_id"])
    title = str(action.get("title") or action.get("text") or "").strip()
    if not title:
        return {"ok": False, "detail": "workspace_add needs a `title`"}
    item = await W.create_item(
        project_id, _ws_actor(assistant),
        kind=str(action.get("kind") or "task"),
        title=title[:W.MAX_TITLE],
        body_md=str(action.get("text") or "")[:W.MAX_BODY],
        status=str(action.get("status") or "open"))
    return {"ok": True, "item_id": item["id"], "kind": item["kind"], "title": item["title"],
            "detail": f"filed #{item['id']} [{item['kind']}] {item['title'][:80]}"}


async def act_workspace_update(assistant: dict, action: dict) -> dict:
    """Move and/or comment on an existing board item. A status change and a comment in one
    act, because "done, because X" is one thought and splitting it costs a whole beat."""
    project_id = str(assistant["project_id"])
    try:
        item_id = int(action.get("item_id") or 0)
    except (TypeError, ValueError):
        item_id = 0
    if not item_id:
        return {"ok": False, "detail": "workspace_update needs an `item_id`"}
    actor = _ws_actor(assistant)
    status = str(action.get("status") or "").strip()
    text = str(action.get("text") or "").strip()
    if not status and not text:
        return {"ok": False, "detail": "workspace_update needs a `status` and/or `text`"}
    item = None
    if status:
        item = await W.update_item(item_id, project_id, actor, status=status)
        if not item:
            return {"ok": False, "detail": f"no item #{item_id} in this project"}
    if text:
        c = await W.add_comment(item_id, project_id, actor, text[:W.MAX_COMMENT])
        if not c:
            return {"ok": False, "detail": f"no item #{item_id} in this project"}
    item = item or await W.get_item(item_id, project_id)
    return {"ok": True, "item_id": item_id, "status": (item or {}).get("status"),
            "detail": f"#{item_id} -> {(item or {}).get('status')}"
                      + (" + comment" if text else "")}


async def act_message(assistant: dict, action: dict,
                      beat_id: Optional[int] = None) -> dict:
    """Send a DM to a colleague on this project (phase 33). No capability gate.

    Ungated for the same reason the board is: this is how an assistant reaches the one
    colleague who CAN do the thing it cannot, and it touches no code and leaves no project.
    What it does cost is a beat — so every bound lives in `messaging.send`, and the refusals
    it returns come back as an action RESULT the human can read, not as a silent no-op.
    """
    project_id = str(assistant["project_id"])
    body = str(action.get("text") or action.get("message") or "").strip()
    if not body:
        return {"ok": False, "detail": "message needs `text`"}
    to_ref = action.get("to") or action.get("assistant") or action.get("title") or ""
    to = await M.resolve(project_id, to_ref)
    if not to:
        names = ", ".join(
            f"{c['name'] or c['role']}" for c in await M.roster(
                project_id, exclude_id=int(assistant["id"]))) or "(nobody else)"
        return {"ok": False,
                "detail": f"no colleague matching {str(to_ref)[:60]!r}. On this project: {names}"}
    item_id = action.get("item_id") or action.get("refs_item_id")
    try:
        item_id = int(item_id) if item_id else None
    except (TypeError, ValueError):
        item_id = None

    # THE FILE-THEN-TELL REPAIR. An assistant reasons once per beat and emits both actions in
    # the same reply, so `{"type":"workspace_add"} , {"type":"message","item_id":…}` — the
    # single most valuable pair there is — names an id that did not exist when it was written.
    # The first real run did exactly this: filed #21, messaged "see item #4". Substitute what
    # this assistant actually just filed, and say so in the result rather than pretending.
    swapped_from = None
    if item_id and not await W.get_item(item_id, project_id):
        recent = await W.latest_item_by(project_id, _ws_actor(assistant).ident)
        if recent:
            swapped_from, item_id = item_id, int(recent["id"])

    res = await M.send(project_id, sender=assistant, to=to, body_md=body,
                       refs_item_id=item_id, beat_id=beat_id)
    if not res.get("ok"):
        return res
    msg = res.get("message") or {}
    note = (f" (you referenced #{swapped_from}, which does not exist — sent it with #{item_id},"
            " the item you filed this beat)" if swapped_from else "")
    return {"ok": True, "message_id": msg.get("id"), "to": msg.get("to_name"),
            "refs_item_id": item_id, "blocked": res.get("blocked") or "",
            "woke": bool(res.get("woke")),
            "detail": f"-> {msg.get('to_name')}: {res.get('detail')}{note}"}


async def apply_action(assistant: dict, action: dict,
                       beat_id: Optional[int] = None) -> dict:
    """Dispatch one action. A denied capability is a recorded action RESULT, not a crash —
    the beat still gets written, and the refusal is visible in the timeline."""
    kind = str((action or {}).get("type") or "").strip().lower()
    try:
        if kind in ("comment", "note", "report"):
            return await act_comment(assistant, str(action.get("text") or ""))
        if kind in ("run_qa", "qa"):
            return await act_run_qa(assistant)
        if kind in ("request_deploy", "deploy"):
            return await act_request_deploy(
                assistant, str(action.get("request") or action.get("text") or ""),
                beat_id=beat_id)
        if kind in ("read_costs", "costs"):
            return await act_costs(assistant)
        if kind == "none":
            return {"ok": True, "detail": "nothing to do this beat"}
        if kind == "check":
            return await act_check(assistant, str(action.get("capability") or ""))
        # The shared work-tracker (phase 32). Ungated on purpose — see `_action_menu`.
        if kind in ("workspace_add", "workspace_new", "ws_add"):
            return await act_workspace_add(assistant, action)
        if kind in ("workspace_update", "workspace_comment", "ws_update"):
            return await act_workspace_update(assistant, action)
        # messaging a colleague (phase 33). Ungated on purpose — see `_action_menu`.
        if kind in ("message", "dm", "msg", "send_message"):
            return await act_message(assistant, action, beat_id=beat_id)
        return {"ok": False, "detail": f"unknown action type {kind!r}"}
    except A.Denied as e:
        return {"ok": False, "denied": True, "detail": str(e)}
    except Exception as e:  # noqa: BLE001
        logger.exception("assistant action %s failed", kind)
        return {"ok": False, "detail": _redact(str(e))[:500]}
