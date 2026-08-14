"""Resumable step engine (phase 12, minimal).

A Run = an ordered list of Steps; each Step = (name, async fn(ctx)). The engine records a
`pipeline_steps` row per step (running -> done|failed), streams an SSE event via ctx.emit,
and writes a per-project current_work.json after every step so a human/fresh session sees
exactly where it is. On resume, steps already `done` for the run are skipped.
"""
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from server import store

logger = logging.getLogger(__name__)

WORK_ROOT = Path(os.environ.get("WORKSPACES_ROOT", "/opt/builderapps/workspaces"))

# ---- per-step hard timeout -------------------------------------------------
# A wedged LLM/docker call must fail the STEP, never wedge the whole run forever (an
# OpenRouter hang once left a run "running" for over an hour with nothing in flight). The
# heartbeat says "the process is alive"; this says "this step has taken absurdly long".
# Generous by design — LLM codegen + a docker build are legitimately slow.
_DEFAULT_STEP_TIMEOUT = float(os.environ.get("BUILDERAPPS_STEP_TIMEOUT_SEC", "1800"))
_STEP_TIMEOUTS: dict[str, float] = {
    "ensure_gitea_account": 300,
    "create_repo": 300,
    "checkout": 600,
    "allocate_secrets": 120,
    "parse_backlog": 120,
    "finalize": 120,
    "update_context": 600,
    "commit_push": 600,
    "strategy_artifacts": 2700,   # six LLM docs in one step
    "data_layer": 1200,
    "deploy_skeleton": 1800,
    "final_deploy": 1800,
    "deploy": 1800,
    "runtime_qa": 2400,
    "plan_and_apply": 1800,
}


def step_timeout(name: str) -> float:
    if name in _STEP_TIMEOUTS:
        return _STEP_TIMEOUTS[name]
    if name.startswith("build_"):     # LLM codegen + docker rebuild + health gate
        return 2400.0
    return _DEFAULT_STEP_TIMEOUT


@dataclass
class Ctx:
    project_id: str
    run_id: int
    emit: Callable[[dict], None]          # push an SSE event dict to the client
    state: dict = field(default_factory=dict)   # shared across steps

    def log(self, msg: str) -> None:
        logger.info("[%s run=%s] %s", self.project_id, self.run_id, msg)


Step = tuple[str, Callable[[Ctx], Awaitable[Any]]]


def _write_current_work(project_id: str, run_id: int, steps: list[Step],
                        idx: int, status: str, extra: Optional[dict] = None) -> None:
    """Mirror the runtime state to <workspace>/current_work.json (best-effort)."""
    try:
        d = WORK_ROOT / project_id
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "project_id": project_id,
            "run_id": run_id,
            "current_step": idx,
            "total_steps": len(steps),
            "step_name": steps[idx][0] if idx < len(steps) else "",
            "status": status,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if extra:
            payload.update(extra)
        (d / "current_work.json").write_text(json.dumps(payload, indent=2), "utf-8")
    except Exception as e:  # noqa: BLE001
        logger.info("current_work.json write skipped: %s", e)


# ctx.state carries the project's db_password, app_secret and the user's Gitea token so the
# steps can use them. The terminal `done` SSE event must NOT ship them to the browser (they
# would land in its memory, in any client-side log, and in whatever captures the stream) —
# the owner has a dedicated, masked /secrets endpoint for that.
_SECRETISH = re.compile(r"(password|secret|token|key|credential)", re.I)


def _public_state(state: dict) -> dict:
    return {k: v for k, v in state.items()
            if isinstance(v, (str, int, bool)) and not _SECRETISH.search(k)}


async def _already_done(run_id: int, idx: int) -> bool:
    row = await store.get_run_with_steps(run_id)
    if not row:
        return False
    for s in row["steps"]:
        if s["idx"] == idx and s["status"] == "done":
            return True
    return False


async def run_partial(project_id: str, run_id: int, steps: list[Step],
                      emit: Callable[[dict], None], *, base_idx: int = 0,
                      state: Optional[dict] = None, finish: bool = False,
                      total: Optional[int] = None) -> dict:
    """Execute `steps` whose engine indices start at `base_idx` (so a run can be assembled in
    two passes — e.g. the create pipeline computes its backlog after the strategy step and then
    appends the remaining steps under the same run_id). Persists/streams exactly like `run`.

    `state` carries ctx.state across passes. When `finish=True`, marks the run done + emits the
    terminal `done` event. Raises (resumable) on a failed step.
    """
    ctx = Ctx(project_id=project_id, run_id=run_id, emit=emit)
    if state:
        ctx.state = state
    if base_idx == 0:
        emit({"type": "run_start", "project_id": project_id, "run_id": run_id,
              "total_steps": total if total is not None else len(steps)})

    for local, (name, fn) in enumerate(steps):
        idx = base_idx + local
        if await _already_done(run_id, idx):
            ctx.log(f"skip done step {idx} {name}")
            emit({"type": "step_done", "idx": idx, "name": name, "skipped": True})
            continue

        await store.upsert_step(run_id, idx, name, "running")
        _write_current_work(project_id, run_id, [("", None)] * (idx + 1), idx, "running",
                            {"step_name": name, "total_steps": total or (base_idx + len(steps))})
        emit({"type": "step_start", "idx": idx, "name": name})
        t0 = time.time()
        try:
            result = await asyncio.wait_for(fn(ctx), timeout=step_timeout(name))
            log = ""
            if isinstance(result, str):
                log = result
            elif isinstance(result, dict):
                log = json.dumps(result)[:2000]
            await store.upsert_step(run_id, idx, name, "done", log)
            _write_current_work(project_id, run_id, [("", None)] * (idx + 1), idx, "done",
                                {"step_name": name})
            emit({"type": "step_done", "idx": idx, "name": name,
                  "ms": int((time.time() - t0) * 1000), "detail": log[:400]})
        except asyncio.CancelledError:
            # The container is going down (uvicorn cancels the task on SIGTERM). This is NOT
            # a step failure: leave the step `running` and the run `running` so the next
            # boot's sweep resumes it from exactly here. Swallowing this as a generic
            # Exception is what used to strand a run silently.
            logger.warning("step %s cancelled — leaving run %s resumable", name, run_id)
            _write_current_work(project_id, run_id, [("", None)] * (idx + 1), idx, "running",
                                {"step_name": name, "interrupted": True})
            raise
        except asyncio.TimeoutError:
            msg = (f"step timed out after {step_timeout(name):.0f}s "
                   f"(hung LLM/build call) — failing the step instead of wedging the run")
            logger.error("step %s: %s", name, msg)
            await store.upsert_step(run_id, idx, name, "failed", msg)
            _write_current_work(project_id, run_id, [("", None)] * (idx + 1), idx, "failed",
                                {"step_name": name, "error": msg})
            emit({"type": "error", "idx": idx, "name": name, "message": msg})
            await store.finish_run(run_id, "failed", msg)
            raise RuntimeError(f"{name}: {msg}")
        except Exception as e:  # noqa: BLE001
            logger.exception("step %s failed", name)
            await store.upsert_step(run_id, idx, name, "failed", str(e)[:8000])
            _write_current_work(project_id, run_id, [("", None)] * (idx + 1), idx, "failed",
                                {"step_name": name, "error": str(e)[:500]})
            emit({"type": "error", "idx": idx, "name": name, "message": str(e)})
            await store.finish_run(run_id, "failed", f"{name}: {e}")
            raise

    if finish:
        await store.finish_run(run_id, "done")
        emit({"type": "done", "project_id": project_id, "run_id": run_id,
              "state": _public_state(ctx.state)})
    return ctx.state


async def run(project_id: str, run_id: int, steps: list[Step],
              emit: Callable[[dict], None]) -> dict:
    """Execute the steps in order (single-pass). Returns ctx.state; raises on a failed step
    (the run is left resumable — done steps are persisted)."""
    return await run_partial(project_id, run_id, steps, emit, base_idx=0,
                             finish=True, total=len(steps))
