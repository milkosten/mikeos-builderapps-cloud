"""Resumable step engine (phase 12, minimal).

A Run = an ordered list of Steps; each Step = (name, async fn(ctx)). The engine records a
`pipeline_steps` row per step (running -> done|failed), streams an SSE event via ctx.emit,
and writes a per-project current_work.json after every step so a human/fresh session sees
exactly where it is. On resume, steps already `done` for the run are skipped.
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from server import store

logger = logging.getLogger(__name__)

WORK_ROOT = Path(os.environ.get("WORKSPACES_ROOT", "/opt/builderapps/workspaces"))


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


async def _already_done(run_id: int, idx: int) -> bool:
    row = await store.get_run_with_steps(run_id)
    if not row:
        return False
    for s in row["steps"]:
        if s["idx"] == idx and s["status"] == "done":
            return True
    return False


async def run(project_id: str, run_id: int, steps: list[Step],
              emit: Callable[[dict], None]) -> dict:
    """Execute the steps in order. Returns the shared ctx.state on success; raises on a
    failed step (the run is left resumable — done steps are persisted)."""
    ctx = Ctx(project_id=project_id, run_id=run_id, emit=emit)
    emit({"type": "run_start", "project_id": project_id, "run_id": run_id,
          "total_steps": len(steps)})

    for idx, (name, fn) in enumerate(steps):
        if await _already_done(run_id, idx):
            ctx.log(f"skip done step {idx} {name}")
            emit({"type": "step_done", "idx": idx, "name": name, "skipped": True})
            continue

        await store.upsert_step(run_id, idx, name, "running")
        _write_current_work(project_id, run_id, steps, idx, "running")
        emit({"type": "step_start", "idx": idx, "name": name})
        t0 = time.time()
        try:
            result = await fn(ctx)
            log = ""
            if isinstance(result, str):
                log = result
            elif isinstance(result, dict):
                log = json.dumps(result)[:2000]
            await store.upsert_step(run_id, idx, name, "done", log)
            _write_current_work(project_id, run_id, steps, idx, "done")
            emit({"type": "step_done", "idx": idx, "name": name,
                  "ms": int((time.time() - t0) * 1000), "detail": log[:400]})
        except Exception as e:  # noqa: BLE001
            logger.exception("step %s failed", name)
            await store.upsert_step(run_id, idx, name, "failed", str(e)[:8000])
            _write_current_work(project_id, run_id, steps, idx, "failed",
                                {"error": str(e)[:500]})
            emit({"type": "error", "idx": idx, "name": name, "message": str(e)})
            await store.finish_run(run_id, "failed")
            raise

    await store.finish_run(run_id, "done")
    emit({"type": "done", "project_id": project_id, "run_id": run_id,
          "state": {k: v for k, v in ctx.state.items() if isinstance(v, (str, int, bool))}})
    return ctx.state
