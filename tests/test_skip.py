"""Offline tests for non-fatal feature steps (phase 28).

Drives `server.harness.engine` with a FAKE store (in-memory rows) so the reliability contract
is proven without a database, a container or a token:

  * a `build_NN` step that raises `StepSkipped` is recorded `skipped` and the run CONTINUES;
  * a `build_NN` step that raises anything else is ALSO skipped (after a revert);
  * a CRITICAL step (data_layer, final_deploy, ...) still fails the whole run;
  * the terminal `done` event carries an honest "N of M features built; K skipped: ..." line;
  * a resumed run does not re-run a skipped feature, and still counts it in the summary.

    python3 -m tests.test_skip
"""
import asyncio
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="skiptest-")
os.environ["WORKSPACES_ROOT"] = os.path.join(_TMP, "workspaces")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import store, workspace  # noqa: E402
from server.harness import engine  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed.append(name)
        print(f"  FAIL {name}  {detail}")


# ---- fake store ------------------------------------------------------------
STEPS: dict[int, dict] = {}
RUN: dict = {"status": "running", "error": "", "summary": "", "total_steps": 0}


async def _upsert_step(run_id, idx, name, status, log=""):
    STEPS[idx] = {"idx": idx, "name": name, "status": status, "log": log}


async def _get_run_with_steps(run_id):
    return {"steps": [dict(s) for s in sorted(STEPS.values(), key=lambda s: s["idx"])]}


async def _finish_run(run_id, status, error="", summary=""):
    RUN.update({"status": status, "error": error, "summary": summary})


async def _noop(*a, **k):
    return None


async def main() -> int:
    store.upsert_step = _upsert_step
    store.get_run_with_steps = _get_run_with_steps
    store.finish_run = _finish_run
    workspace.revert_uncommitted = _noop           # nothing to revert in a fake tree

    events: list[dict] = []

    def emit(ev):
        events.append(ev)

    async def ok(ctx):
        return "fine"

    def boom(kind: str):
        async def f(ctx):
            if kind == "skip":
                raise engine.StepSkipped("deploy health gate failed", label="CSV export")
            raise RuntimeError("unexpected explosion")
        return f

    # --- 1. a skippable feature failure does not kill the run ---------------
    print("\n[skip-and-continue]")
    STEPS.clear()
    events.clear()
    RUN.update({"status": "running", "summary": ""})
    steps = [("data_layer", ok), ("build_01", ok), ("build_02", boom("skip")),
             ("build_03", boom("raise")), ("build_04", ok), ("final_deploy", ok)]
    state = await engine.run_partial("abc123", 1, steps, emit, base_idx=0,
                                     state={"feature_total": 4}, finish=True,
                                     total=len(steps))
    check("run finished done despite two failed features", RUN["status"] == "done", RUN)
    check("build_02 recorded skipped", STEPS[2]["status"] == "skipped", STEPS.get(2))
    check("build_03 (unexpected error) also skipped", STEPS[3]["status"] == "skipped",
          STEPS.get(3))
    check("later features still ran", STEPS[4]["status"] == "done", STEPS.get(4))
    check("final_deploy still ran", STEPS[5]["status"] == "done", STEPS.get(5))
    check("two skips carried in state", len(state["skipped"]) == 2, state.get("skipped"))
    check("summary is honest 2 of 4",
          RUN["summary"].startswith("2 of 4 features built; 2 skipped:"), RUN["summary"])
    check("skip reason names the feature", "CSV export" in RUN["summary"], RUN["summary"])
    done = [e for e in events if e.get("type") == "done"]
    check("terminal done event carries the summary",
          bool(done) and done[0].get("summary") == RUN["summary"], done)
    check("terminal done event lists the skipped features",
          bool(done) and len(done[0].get("skipped") or []) == 2, done)
    check("a step_skipped event was emitted per skip",
          len([e for e in events if e.get("type") == "step_skipped"]) == 2, events)
    check("no secret leaks into the done state",
          all(k in ("feature_total",) or "secret" not in k
              for k in (done[0].get("state") or {})), done)

    # --- 2. a critical step is still fatal ----------------------------------
    print("\n[critical steps stay fatal]")
    for critical in ("data_layer", "final_deploy", "checkout", "plan_and_apply"):
        STEPS.clear()
        events.clear()
        RUN.update({"status": "running", "summary": ""})
        raised = False
        try:
            await engine.run_partial("abc123", 2, [(critical, boom("raise")), ("build_01", ok)],
                                     emit, base_idx=0, state={"feature_total": 1},
                                     finish=True, total=2)
        except Exception:  # noqa: BLE001
            raised = True
        check(f"{critical} failure raises", raised)
        check(f"{critical} failure marks the run failed", RUN["status"] == "failed", RUN)
        check(f"{critical} is not skippable", not engine.is_skippable(critical))
    check("build_07 IS skippable", engine.is_skippable("build_07"))
    check("build_12_fix is skippable", engine.is_skippable("build_12_fix"))
    check("runtime_qa is not skippable", not engine.is_skippable("runtime_qa"))

    # --- 3. resume does not re-run a skipped feature ------------------------
    print("\n[resume]")
    STEPS.clear()
    events.clear()
    RUN.update({"status": "running", "summary": ""})
    STEPS[0] = {"idx": 0, "name": "build_01", "status": "done", "log": ""}
    STEPS[1] = {"idx": 1, "name": "build_02", "status": "skipped", "log": "failed twice (boom)"}
    ran: list[str] = []

    async def track(ctx):
        ran.append("ran")
        return "fine"

    await engine.run_partial("abc123", 3,
                             [("build_01", track), ("build_02", track), ("build_03", track)],
                             emit, base_idx=0, state={"feature_total": 3}, finish=True,
                             total=3)
    check("resume ran only the outstanding step", len(ran) == 1, ran)
    check("resume kept the skip recorded", STEPS[1]["status"] == "skipped", STEPS.get(1))
    check("resume summary still counts the earlier skip",
          RUN["summary"].startswith("2 of 3 features built; 1 skipped:"), RUN["summary"])

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        print("failed: " + ", ".join(_failed))
    return 1 if _failed else 0


if __name__ == "__main__":
    import shutil
    try:
        rc = asyncio.run(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(rc)
