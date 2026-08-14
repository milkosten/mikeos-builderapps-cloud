"""Token/cost accounting — bridges `gpu`'s per-call usage records to the database.

`gpu` doesn't know which project it is generating for, and threading that through every
call signature would touch a lot of code for a cosmetic feature. Instead the harness sets
the current project/run/step in a ContextVar and installs a sink; each LLM call then lands
as one `builderapps.llm_usage` row.

Accounting must NEVER break a build: every path here swallows its own errors, and rows are
written on a fire-and-forget task so an LLM call is never blocked on a DB write.
"""
import asyncio
import contextlib
import contextvars
import logging
from typing import Any, Dict, Iterator, List, Optional

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


# Per-CALLER capture (phase 29). An assistant beat must report what THAT beat cost, and the
# global sink only knows how to write rows. A ContextVar bucket is the right tool: it is set
# in the calling task, so the synchronous sink invoked from inside the awaited LLM call sees
# it, and concurrent builds in other tasks cannot leak into it.
_capture: contextvars.ContextVar[Optional[List[Dict[str, Any]]]] = \
    contextvars.ContextVar("usage_capture", default=None)


@contextlib.contextmanager
def capture() -> Iterator[List[Dict[str, Any]]]:
    """Collect the accounting records produced by LLM calls inside this block.

        with usage.capture() as recs:
            await gpu.chat(...)
        cost = sum(r["cost_usd"] for r in recs)

    Purely additive: the rows are still written by the normal sink."""
    bucket: List[Dict[str, Any]] = []
    tok = _capture.set(bucket)
    try:
        yield bucket
    finally:
        _capture.reset(tok)


def _sink(rec: Dict[str, Any]) -> None:
    bucket = _capture.get()
    if bucket is not None:
        bucket.append(dict(rec))
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
