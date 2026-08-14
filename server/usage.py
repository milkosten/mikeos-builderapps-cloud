"""Token/cost accounting — bridges `gpu`'s per-call usage records to the database.

`gpu` doesn't know which project it is generating for, and threading that through every
call signature would touch a lot of code for a cosmetic feature. Instead the harness sets
the current project/run/step in a ContextVar and installs a sink; each LLM call then lands
as one `builderapps.llm_usage` row.

Accounting must NEVER break a build: every path here swallows its own errors, and rows are
written on a fire-and-forget task so an LLM call is never blocked on a DB write.
"""
import asyncio
import contextvars
import logging
from typing import Any, Dict, Optional

from server import gpu, store

logger = logging.getLogger(__name__)

# (project_id, run_id, step) for whatever the harness is currently doing.
_ctx: contextvars.ContextVar[tuple] = contextvars.ContextVar("usage_ctx", default=(None, None, None))

# Strong refs so fire-and-forget writes can't be garbage-collected mid-flight
# (this codebase has been bitten by that before).
_tasks: set = set()


def set_context(project_id: Optional[str], run_id: Optional[int], step: Optional[str]) -> None:
    _ctx.set((project_id, run_id, step))


def clear_context() -> None:
    _ctx.set((None, None, None))


def _sink(rec: Dict[str, Any]) -> None:
    project_id, run_id, step = _ctx.get()
    if not project_id:
        return                      # a call outside any project (e.g. a healthcheck) — ignore
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(_write(project_id, run_id, step, rec))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _write(project_id: str, run_id: Optional[int], step: Optional[str],
                 rec: Dict[str, Any]) -> None:
    try:
        await store.record_usage(
            project_id=project_id, run_id=run_id, step=step,
            model=rec.get("model") or "", prompt_tokens=int(rec.get("prompt_tokens") or 0),
            completion_tokens=int(rec.get("completion_tokens") or 0),
            cached_tokens=int(rec.get("cached_tokens") or 0),
            cost_usd=float(rec.get("cost_usd") or 0.0),
            cost_estimated=bool(rec.get("cost_estimated")),
        )
    except Exception:
        logger.debug("usage write failed for %s", project_id, exc_info=True)


def install() -> None:
    """Called once at startup."""
    gpu.set_usage_sink(_sink)
