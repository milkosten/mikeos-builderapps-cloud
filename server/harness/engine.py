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


# ---- skippable vs critical steps (phase 28) --------------------------------
# One failed feature used to kill a 23-step run and the user lost the WHOLE app. A feature is
# now retried (pipeline side, with the error fed back into the agentic loop) and, if it still
# fails, marked `skipped` so the build continues with the features that DO work.
#
# Only `build_NN` features are skippable. Everything else is load-bearing: you cannot skip the
# repo, the checkout, the secrets, the skeleton deploy, the DATA LAYER or the final deploy and
# still have an app — those stay fatal on purpose.
CRITICAL_STEPS = frozenset({
    "ensure_gitea_account", "create_repo", "checkout", "allocate_secrets",
    "deploy_skeleton", "strategy_artifacts", "parse_backlog", "data_layer",
    "final_deploy", "finalize",
    # update pipeline
    "update_context", "plan_and_apply", "deploy", "commit_push",
})
_FEATURE_RE = re.compile(r"^build_\d+")


def is_skippable(name: str) -> bool:
    """True only for a backlog FEATURE step. Anything critical stays fatal."""
    return bool(_FEATURE_RE.match(name)) and name not in CRITICAL_STEPS


class StepSkipped(Exception):
    """A skippable step gave up after its retry. Carries the honest reason.

    Raised by the step function itself once it has cleaned up after the failure (reverted the
    broken partial work and put the last-good build back on the air), so the engine can record
    `skipped` and move to the next feature knowing the app is still deployable.
    """

    def __init__(self, reason: str, label: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.label = label


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


async def _prior_steps(run_id: int) -> dict[int, dict]:
    """idx -> step row for everything this run has already recorded.

    `done` AND `skipped` are both terminal: a resumed run must not re-run a feature that
    already gave up (it would burn the whole budget again), and it must remember the skip so
    the final summary stays honest.
    """
    row = await store.get_run_with_steps(run_id)
    if not row:
        return {}
    return {int(s["idx"]): dict(s) for s in row["steps"]}


def run_summary(state: dict) -> str:
    """The honest one-line outcome for a run. Never claims success when features were skipped."""
    skipped = state.get("skipped") or []
    total = int(state.get("feature_total") or 0)
    if not skipped:
        return (f"{total} of {total} features built" if total else "all steps completed")
    names = "; ".join(f"{s.get('label') or s.get('step')} ({s.get('reason', '')[:120]})"
                      for s in skipped)
    built = max(total - len(skipped), 0)
    return (f"{built} of {total} features built; {len(skipped)} skipped: {names}"
            if total else f"{len(skipped)} step(s) skipped: {names}")


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
    ctx.state.setdefault("skipped", [])
    if base_idx == 0:
        emit({"type": "run_start", "project_id": project_id, "run_id": run_id,
              "total_steps": total if total is not None else len(steps)})

    prior = await _prior_steps(run_id)
    # A resumed run inherits the skips it already recorded, so the final summary still counts
    # them (they are not re-run and would otherwise vanish from the report).
    known_skips = {s.get("step") for s in ctx.state["skipped"]}
    for row in prior.values():
        if row.get("status") == "skipped" and row.get("name") not in known_skips:
            ctx.state["skipped"].append({"step": row.get("name"), "label": row.get("name"),
                                         "reason": (row.get("log") or "")[:300]})

    for local, (name, fn) in enumerate(steps):
        idx = base_idx + local
        prev = prior.get(idx) or {}
        if prev.get("status") == "done":
            ctx.log(f"skip done step {idx} {name}")
            emit({"type": "step_done", "idx": idx, "name": name, "skipped": True})
            continue
        if prev.get("status") == "skipped":
            ctx.log(f"step {idx} {name} was already skipped — not retrying")
            emit({"type": "step_skipped", "idx": idx, "name": name,
                  "reason": (prev.get("log") or "")[:400]})
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
        except StepSkipped as e:
            # The step gave up but cleaned up after itself (see pipeline._s_feature): the
            # workspace is back at the last good commit and the last-good build is on the air.
            # Record it, tell the client honestly, and keep building the rest of the app.
            await _record_skip(ctx, idx, name, e.reason, e.label or name)
            continue
        except asyncio.TimeoutError:
            msg = (f"step timed out after {step_timeout(name):.0f}s "
                   f"(hung LLM/build call) — failing the step instead of wedging the run")
            logger.error("step %s: %s", name, msg)
            if is_skippable(name):
                await _record_skip(ctx, idx, name, msg, name)
                continue
            await store.upsert_step(run_id, idx, name, "failed", msg)
            _write_current_work(project_id, run_id, [("", None)] * (idx + 1), idx, "failed",
                                {"step_name": name, "error": msg})
            emit({"type": "error", "idx": idx, "name": name, "message": msg})
            await store.finish_run(run_id, "failed", msg, run_summary(ctx.state))
            raise RuntimeError(f"{name}: {msg}")
        except Exception as e:  # noqa: BLE001
            logger.exception("step %s failed", name)
            if is_skippable(name):
                # A feature that failed WITHOUT cleaning up (an unexpected error rather than a
                # give-up) is still not worth the whole app — skip it too, but only after
                # putting the tree back to its last good state so the next feature builds on
                # something that works.
                await _safe_revert(ctx)
                await _record_skip(ctx, idx, name, str(e)[:300], name)
                continue
            await store.upsert_step(run_id, idx, name, "failed", str(e)[:8000])
            _write_current_work(project_id, run_id, [("", None)] * (idx + 1), idx, "failed",
                                {"step_name": name, "error": str(e)[:500]})
            emit({"type": "error", "idx": idx, "name": name, "message": str(e)})
            await store.finish_run(run_id, "failed", f"{name}: {e}", run_summary(ctx.state))
            raise

    if finish:
        summary = run_summary(ctx.state)
        await store.finish_run(run_id, "done", "", summary)
        emit({"type": "done", "project_id": project_id, "run_id": run_id,
              "summary": summary,
              "skipped": [{"step": s.get("step"), "feature": s.get("label"),
                           "reason": s.get("reason", "")[:300]}
                          for s in (ctx.state.get("skipped") or [])],
              "state": _public_state(ctx.state)})
    return ctx.state


async def _record_skip(ctx: Ctx, idx: int, name: str, reason: str, label: str) -> None:
    """Persist + announce a skipped step. The run stays alive."""
    logger.warning("step %s SKIPPED: %s", name, reason)
    ctx.state.setdefault("skipped", []).append(
        {"step": name, "label": label, "reason": reason[:300]})
    await store.upsert_step(ctx.run_id, idx, name, "skipped", reason[:8000])
    _write_current_work(ctx.project_id, ctx.run_id, [("", None)] * (idx + 1), idx, "skipped",
                        {"step_name": name, "skipped_reason": reason[:500]})
    ctx.emit({"type": "step_skipped", "idx": idx, "name": name, "reason": reason[:400]})
    # Older clients only understand the create/update vocabulary — make sure the skip is
    # visible there too rather than looking like a step that silently vanished.
    ctx.emit({"type": "progress", "stage": "skipped",
              "detail": f"{label}: skipped — {reason[:200]}"})


async def _safe_revert(ctx: Ctx) -> None:
    """Drop uncommitted (broken) work so the next step starts from the last good commit."""
    try:
        from server import workspace
        await workspace.revert_uncommitted(ctx.project_id)
    except Exception as e:  # noqa: BLE001
        logger.info("revert after skip failed for %s: %s", ctx.project_id, e)


async def run(project_id: str, run_id: int, steps: list[Step],
              emit: Callable[[dict], None]) -> dict:
    """Execute the steps in order (single-pass). Returns ctx.state; raises on a failed step
    (the run is left resumable — done steps are persisted)."""
    return await run_partial(project_id, run_id, steps, emit, base_idx=0,
                             finish=True, total=len(steps))
