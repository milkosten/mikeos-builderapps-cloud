"""Durable pipeline runner (phase 19) — execution is DECOUPLED from the HTTP request.

The bug this exists to kill: `POST /api/projects` used to `await` the pipeline inside its own
SSE response generator. So the run's lifetime was the request's lifetime, and
`docker compose up -d --build mikeos-builderapps` (every control-plane deploy) — or the user
simply closing the tab — silently killed every in-flight build. Nothing detected it: the row
stayed `status='running'` forever and the project stayed `creating`/`deploying`. Five real
customer builds were orphaned that way.

The model here:

* **run == a background task, not a request.** `start()` spawns the pipeline as an
  `asyncio.Task` held in a module-level set (strong ref — a bare `create_task` can be
  garbage-collected mid-flight; this codebase has been bitten by that).
* **the request only OBSERVES.** Events are published to an in-process broker; the SSE
  endpoint subscribes to it. A disconnect unsubscribes and nothing more — the build carries on.
* **liveness is a heartbeat in Postgres.** While a run is alive its owner bumps
  `pipeline_runs.heartbeat_at` every ~30s. That single fact is what makes "is this run dead?"
  answerable from a *different* process.
* **boot sweep + janitor.** On startup (and every ~90s after) any `running` run with a stale
  heartbeat is CLAIMED (atomic UPDATE, so two workers can't both take it) and RESUMED — the
  engine is resumable, `done` steps are skipped. A run that can't resume is marked `failed`
  with a reason, and its project with it. A run is never left in limbo.
"""
import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from server import store

logger = logging.getLogger(__name__)

# Identifies THIS api process. Includes a boot uuid so a restarted container with the same
# pid can never be mistaken for the process that owned a run before the restart.
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

HEARTBEAT_SEC = float(os.environ.get("BUILDERAPPS_HEARTBEAT_SEC", "30"))
# How long without a heartbeat before a run is presumed dead. Must be comfortably larger than
# HEARTBEAT_SEC so a GC pause / slow query can't cause a spurious takeover.
STALE_SEC = int(os.environ.get("BUILDERAPPS_RUN_STALE_SEC", "180"))
JANITOR_SEC = float(os.environ.get("BUILDERAPPS_JANITOR_SEC", "90"))
# Bounded retries: a run that keeps dying must eventually fail loudly instead of looping.
# A step that FAILS already terminates the run, so this really bounds "how many process
# restarts may one run survive" — generous enough for a long build to ride out a few deploys,
# small enough that a pathological run can't be resumed forever.
MAX_ATTEMPTS = int(os.environ.get("BUILDERAPPS_RUN_MAX_ATTEMPTS", "6"))
# Events kept per run so a client that reconnects mid-build still sees what it missed.
_HISTORY_CAP = 500
_HISTORY_RUNS = 200      # how many runs' buffers to keep in memory at once
# A boot sweep can find many orphans at once (a redeploy during a busy hour). Each resumed run
# means LLM calls + docker builds, so recovery is throttled — it must never stampede the box
# and take the live control plane down with it. Interactive creates are NOT gated: recovery is
# background work and must yield to a user who is watching a build right now.
_RESUME_SLOTS = asyncio.Semaphore(int(os.environ.get("BUILDERAPPS_RESUME_CONCURRENCY", "3")))

# run_id -> the task actually executing the pipeline (STRONG refs; never GC'd mid-flight)
_active: dict[int, asyncio.Task] = {}
# run_id -> live SSE subscriber queues
_subs: dict[int, set[asyncio.Queue]] = {}
# run_id -> bounded replay buffer
_history: dict[int, list[dict]] = {}

_janitor_task: Optional[asyncio.Task] = None
_shutting_down = False


# ---------------------------------------------------------------------------
# event broker
# ---------------------------------------------------------------------------
def publish(run_id: int, event: dict) -> None:
    """Fan an event out to every current subscriber (and remember it for reconnects).
    Never blocks and never raises — a slow/dead client must not stall a build."""
    buf = _history.setdefault(run_id, [])
    buf.append(event)
    if len(buf) > _HISTORY_CAP:
        del buf[: len(buf) - _HISTORY_CAP]
    # Bound total memory: forget the oldest FINISHED, unwatched runs. Postgres remains the
    # durable record (`/steps`); this buffer only exists for a quick SSE re-attach.
    if len(_history) > _HISTORY_RUNS:
        for old in [r for r in list(_history)[:-_HISTORY_RUNS]
                    if r not in _subs and not is_active(r)]:
            _history.pop(old, None)
    for q in list(_subs.get(run_id, ())):
        try:
            q.put_nowait(event)
        except Exception:  # noqa: BLE001 — full/closed queue: drop, the DB is the record
            pass


def emitter(run_id: int) -> Callable[[dict], None]:
    """The `emit` callable handed to the engine."""
    def _emit(event: dict) -> None:
        publish(run_id, event)
    return _emit


def subscribe(run_id: int) -> tuple[asyncio.Queue, list[dict]]:
    """Attach to a run's event stream. Returns (queue, events-so-far) so a late or
    reconnecting client can replay history before following live."""
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subs.setdefault(run_id, set()).add(q)
    return q, list(_history.get(run_id, ()))


def unsubscribe(run_id: int, q: asyncio.Queue) -> None:
    subs = _subs.get(run_id)
    if not subs:
        return
    subs.discard(q)
    if not subs:
        _subs.pop(run_id, None)


def is_active(run_id: int) -> bool:
    t = _active.get(run_id)
    return bool(t and not t.done())


# ---------------------------------------------------------------------------
# running a pipeline durably
# ---------------------------------------------------------------------------
async def _heartbeat_loop(run_id: int) -> None:
    """Bump heartbeat_at until cancelled. If ownership is lost (another process claimed the
    run) we log it — the janitor's claim is atomic, so this should be rare."""
    while True:
        await asyncio.sleep(HEARTBEAT_SEC)
        try:
            if not await store.heartbeat_run(run_id, INSTANCE_ID):
                logger.warning("run %s: heartbeat rejected (ownership lost)", run_id)
        except Exception as e:  # noqa: BLE001 — a transient DB blip must not kill the build
            logger.warning("run %s: heartbeat failed: %s", run_id, e)


async def _supervise(run_id: int, project_id: str,
                     body: Callable[[], Awaitable[Any]]) -> None:
    """Run one pipeline body with a heartbeat alongside it, and guarantee a terminal state.

    Cancellation (uvicorn shutting the container down) is deliberately NOT treated as a
    failure: the run is released so the next boot's sweep resumes it exactly where it stopped.
    """
    hb = asyncio.create_task(_heartbeat_loop(run_id))
    try:
        await body()
        logger.info("run %s (%s) finished", run_id, project_id)
    except asyncio.CancelledError:
        # Container going down mid-build. Leave status='running' + drop ownership so the
        # startup sweep picks it up immediately instead of waiting out the staleness window.
        logger.warning("run %s (%s) cancelled (shutdown) — left resumable", run_id, project_id)
        try:
            await store.release_run(run_id, INSTANCE_ID)
        except Exception:  # noqa: BLE001
            pass
        raise
    except Exception as e:  # noqa: BLE001 — the pipeline already recorded the failed step
        logger.exception("run %s (%s) failed", run_id, project_id)
        publish(run_id, {"type": "error", "message": str(e)[:500]})
        await _mark_failed(run_id, project_id, str(e))
    finally:
        hb.cancel()
        publish(run_id, {"type": "eof"})
        _active.pop(run_id, None)


async def _mark_failed(run_id: int, project_id: str, reason: str) -> None:
    """Terminal-state bookkeeping: run failed, any half-run step failed, project failed."""
    try:
        await store.fail_stuck_steps(run_id, reason[:500])
        run = await store.get_run(run_id)
        if run and run["status"] == "running":
            await store.finish_run(run_id, "failed", reason[:2000])
        proj = await store.get_project(project_id)
        if proj and proj["status"] in ("creating", "deploying", "building"):
            await store.set_project_status(project_id, "failed")
    except Exception:  # noqa: BLE001
        logger.exception("run %s: could not record terminal failure", run_id)


def _spawn(run_id: int, project_id: str, body: Callable[[], Awaitable[Any]]) -> asyncio.Task:
    task = asyncio.create_task(_supervise(run_id, project_id, body))
    _active[run_id] = task          # strong ref for the whole lifetime of the run
    return task


async def start(run_id: int, project_id: str,
                body: Callable[[], Awaitable[Any]]) -> asyncio.Task:
    """Start a freshly created run in the background and return immediately."""
    await store.claim_new_run(run_id, INSTANCE_ID)
    logger.info("run %s (%s): started by %s", run_id, project_id, INSTANCE_ID)
    return _spawn(run_id, project_id, body)


# ---------------------------------------------------------------------------
# recovery: boot sweep + janitor
# ---------------------------------------------------------------------------
async def _resume(run: dict) -> None:
    """Claim + resume one orphaned run. Marks it failed (with a reason) when it cannot be
    resumed, so the UI shows a real error instead of an eternal spinner."""
    from server.harness import pipeline   # local import: pipeline must not import runner

    run_id = int(run["id"])
    project_id = str(run["project_id"])
    if is_active(run_id):
        return

    claimed = await store.claim_run(run_id, INSTANCE_ID, STALE_SEC)
    if not claimed:
        return                      # another worker got there first — leave it alone

    attempts = int(claimed.get("attempts") or 0)
    age_note = f"attempt {attempts}/{MAX_ATTEMPTS}"
    if attempts > MAX_ATTEMPTS:
        reason = (f"pipeline run abandoned after {attempts - 1} recovery attempts — "
                  f"the control plane restarted or the step kept dying")
        logger.error("run %s (%s): %s", run_id, project_id, reason)
        await _mark_failed(run_id, project_id, reason)
        publish(run_id, {"type": "error", "message": reason})
        return

    proj = await store.get_project(project_id)
    if not proj:
        await _mark_failed(run_id, project_id, "project row is gone")
        return
    if proj.get("status") == "destroyed":
        await store.fail_stuck_steps(run_id, "project destroyed")
        await store.finish_run(run_id, "failed", "project was destroyed")
        return

    kind = str(run.get("kind") or "create")
    user_id = str(proj.get("user_id") or "")
    if not user_id:
        await _mark_failed(run_id, project_id, "run has no owning user — cannot resume")
        return

    emit = emitter(run_id)
    request_text = str(run.get("request") or "")

    async def body() -> Any:
        publish(run_id, {"type": "progress", "stage": "resume",
                         "detail": f"resuming interrupted run ({age_note})"})
        # The heartbeat is already running while we wait for a slot, so a queued run still
        # reads as ALIVE and no other process will try to take it over.
        async with _RESUME_SLOTS:
            logger.info("run %s (%s): resuming kind=%s %s", run_id, project_id, kind, age_note)
            if kind == "update":
                if not request_text:
                    raise RuntimeError("update run has no stored request text — cannot resume")
                return await pipeline.run_update(project_id, run_id, user_id, None,
                                                 request_text, emit, resume=True)
            return await pipeline.run_create(project_id, run_id, user_id, None, emit,
                                             resume=True)

    _spawn(run_id, project_id, body)


def _is_stale(run: dict) -> bool:
    hb = run.get("heartbeat_at")
    if hb is None:
        return True
    now = datetime.now(timezone.utc)
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return (now - hb).total_seconds() > STALE_SEC


async def sweep() -> int:
    """Find every orphaned run and resume (or fail) it. Returns how many were touched."""
    if _shutting_down:
        return 0
    try:
        stale = await store.stale_running_runs(STALE_SEC)
    except Exception as e:  # noqa: BLE001
        logger.warning("run sweep query failed: %s", e)
        return 0
    n = 0
    for run in stale:
        if is_active(int(run["id"])):
            continue
        try:
            await _resume(run)
            n += 1
        except Exception:  # noqa: BLE001 — one bad run must not abort the sweep
            logger.exception("could not recover run %s", run.get("id"))
    return n


async def sweep_on_boot() -> int:
    """Startup recovery. Any run marked `running` at boot predates this process by definition
    (nothing else can be executing in it), so orphans are taken over immediately: we treat a
    heartbeat older than the staleness window as dead, and an unowned run as dead outright."""
    try:
        # Unowned (released by a clean shutdown) OR stale (SIGKILL / OOM / crash). A run whose
        # owner is still heartbeating belongs to a live sibling worker — never steal it.
        rows = [r for r in await store.running_runs()
                if not (r.get("owner") or "") or _is_stale(r)]
    except Exception as e:  # noqa: BLE001
        logger.warning("boot sweep query failed: %s", e)
        return 0
    if rows:
        logger.warning("boot sweep: %d interrupted run(s) to recover: %s",
                       len(rows), [int(r["id"]) for r in rows])
    n = 0
    for run in rows:
        try:
            await _resume(run)
            n += 1
        except Exception:  # noqa: BLE001
            logger.exception("boot sweep could not recover run %s", run.get("id"))
    return n


async def _janitor_loop() -> None:
    while not _shutting_down:
        try:
            await asyncio.sleep(JANITOR_SEC)
            await sweep()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the janitor must never die
            logger.exception("janitor iteration failed")


def start_janitor() -> None:
    global _janitor_task
    if _janitor_task is None or _janitor_task.done():
        _janitor_task = asyncio.create_task(_janitor_loop())


async def shutdown() -> None:
    """Cancel the janitor and release ownership of everything still running, so the next boot
    resumes instantly rather than waiting out the staleness window."""
    global _shutting_down, _janitor_task
    _shutting_down = True
    if _janitor_task:
        _janitor_task.cancel()
        _janitor_task = None
    for run_id, task in list(_active.items()):
        if not task.done():
            try:
                await store.release_run(run_id, INSTANCE_ID)
            except Exception:  # noqa: BLE001
                pass
            task.cancel()
