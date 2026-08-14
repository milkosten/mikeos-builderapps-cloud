"""Offline tests for the Build Backlog parser.

The bug these exist to prevent actually shipped: the TECHNICAL-PLAN prompt asked the model for
"6-14 tasks" while the pipeline capped the backlog at 12, so a 14-item plan was parsed and its
last two items were **thrown away in silence**. On the campaign's changelog build those two
items were "Frontend /admin editor page" and "GET /rss.xml feed" — the admin editor and the RSS
feed, two of the three things the user asked for. `/admin` and `/rss.xml` both 404'd on the
finished app, and the run reported "12 of 12 features built".

So: one constant shared with the prompt, and overflow is FOLDED into the last step, never cut.

    python3 -m tests.test_backlog
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.harness import backlog, codegen  # noqa: E402

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


def plan(n: int) -> str:
    items = "\n".join(f"{i}. Task number {i}" for i in range(1, n + 1))
    return f"# Technical Plan\n\n## Routes\n\n- GET /x\n\n## Build Backlog\n\n{items}\n\n## Notes\n\n- ignore me\n"


def main() -> int:
    print("[parsing]")
    got = backlog.parse_backlog(plan(5))
    check("reads the numbered items under the heading", got == [f"Task number {i}" for i in range(1, 6)], got)
    check("stops at the next heading", "ignore me" not in " ".join(got), got)

    bullets = "# P\n\n## Build Backlog\n\n- **Alpha** thing\n- `beta` thing\n"
    got = backlog.parse_backlog(bullets)
    check("accepts bullets and strips bold", got[0] == "Alpha thing", got)
    check("de-dups nothing here", len(got) == 2, got)

    print("\n[the cap folds, it does not truncate]")
    n = backlog.MAX_FEATURES
    got = backlog.parse_backlog(plan(n + 2))
    check("the step count stays at the cap", len(got) == n, len(got))
    check("the overflow is not lost — it rides in the last step",
          f"Task number {n + 1}" in got[-1] and f"Task number {n + 2}" in got[-1], got[-1])
    check("the last kept item is still there too", f"Task number {n}" in got[-1], got[-1])
    check("earlier items are untouched", got[0] == "Task number 1", got[0])

    check("a plan that fits is returned as-is",
          backlog.parse_backlog(plan(n)) == [f"Task number {i}" for i in range(1, n + 1)])
    check("cap=0 means no cap (what the plan actually promised)",
          len(backlog.parse_backlog(plan(n + 5), cap=0)) == n + 5)

    print("\n[the prompt and the cap cannot drift apart]")
    check("the TECHNICAL-PLAN prompt quotes the real cap",
          f"AT MOST {backlog.MAX_FEATURES} " in codegen.TECH_PLAN_ASK, codegen.TECH_PLAN_ASK[:200])
    check("the prompt tells the planner every page needs a backlog item",
          "## Pages` must have its own backlog item" in codegen.TECH_PLAN_ASK)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        print("failed: " + ", ".join(_failed))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
