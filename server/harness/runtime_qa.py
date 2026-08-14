"""Runtime QA (phase 17) — prove the live app works in a real browser and auto-fix what doesn't.

The designer runtime-QA loop, scaled to full-stack apps:
  1. chrome-pool opens the LIVE url, exercises the UI, captures console errors + failed
     network requests + a screenshot; we also tail the <id>-app container logs for server 500s.
  2. SEMANTIC pass (phase 28, `server.harness.semantic_qa`): seed a record through the app's
     OWN API, then assert it actually RENDERS in the browser. Console-only QA once passed an
     app that showed "No links yet" with rows in Postgres, because the client caught its own
     `links.forEach is not a function` and rendered the empty state as text — an empty
     `window.__errs`. Absence of errors is not evidence of a working app.
  3. If anything's wrong, hand the errors AND the broken flows to the agentic loop (phase 26):
     it reads the real code, tails the container's own logs, and lands a bounded,
     syntax-checked edit; then we rebuild+redeploy and re-run.
  4. Loop up to `max_rounds` (2). Stop when clean or the budget is exhausted.
  5. Completeness critic: ask which UX-doc flow is untested/broken and record it (never declare
     done prematurely — never-trust-200 for UX too).

Everything is best-effort about *capturing* but honest about *reporting*: a still-broken app is
marked live-with-warnings, never logged as success.
"""
import asyncio
import logging
import re
from typing import Callable, Dict, List, Optional

from server import chrome, deployer, gpu, workspace
from server.harness import agentic, semantic_qa

logger = logging.getLogger(__name__)


async def _tail_app_logs(shortid: str, tail: int = 80) -> str:
    """Tail the <id>-app container logs and pull out lines that smell like a server error."""
    import asyncio as _a
    proc = await _a.create_subprocess_exec(
        "docker", "logs", "--tail", str(tail), f"{shortid}-app",
        stdout=_a.subprocess.PIPE, stderr=_a.subprocess.STDOUT,
    )
    try:
        out, _ = await _a.wait_for(proc.communicate(), timeout=25)
    except _a.TimeoutError:
        proc.kill()
        return ""
    text = (out or b"").decode("utf-8", "replace")
    bad = [ln for ln in text.splitlines()
           if re.search(r"\b(error|exception|throw|unhandled|ECONN|500|reject|fatal)\b", ln, re.I)]
    return "\n".join(bad[-30:])


async def _autofix(shortid: str, brief: str, tech_plan: str,
                   errors: List[str], network: List[str], server_errs: str,
                   *, run_id: int = 0, round_no: int = 1, semantic: Optional[List[str]] = None,
                   url: str = "", emit: Optional[Callable[[dict], None]] = None) -> List[str]:
    """Localize + fix with the agentic loop. Returns the files it actually changed.

    The agent reads the real code (and can `app_logs` the container itself, and `grep` for the
    handler or DOM id named in the finding) instead of being handed a source dump and asked for
    whole files back — which is what let a QA "fix" quietly rewrite an endpoint's response keys
    and move the bug somewhere else.

    The SEMANTIC findings are the sharpest input here: "the API returns the row but the page
    does not render it" localizes the bug to the client's unwrapping, which is exactly the
    class of failure the console-error-only QA used to pass straight through.
    """
    problem = (
        (f"The live app is {url}\n\n" if url else "")
        + f"Console/JS errors:\n{chr(10).join(errors[:15]) or '(none)'}\n\n"
        f"Failed network requests (status url):\n{chr(10).join(network[:15]) or '(none)'}\n\n"
        f"Server-side error log lines:\n{server_errs[:2000] or '(none)'}\n\n"
        f"END-TO-END FLOW FAILURES (a record was created through the app's own API, then the "
        f"page was rendered in a real browser):\n"
        f"{chr(10).join((semantic or [])[:8]) or '(none)'}\n\n"
        "Localize before you edit: grep for the route or the DOM id named above, read the "
        "handler AND the client code that consumes it, and call app_logs. Fix the smallest "
        "thing that makes the flow work end to end."
    )
    res = await agentic.run_agent(
        project_id=shortid, run_id=run_id, step=f"qa_autofix_r{round_no}",
        brief=brief, tech_plan=tech_plan,
        task="The live app has runtime problems. Find the real cause and fix it with the "
             "smallest possible change.",
        extra=problem, recent_changes=["runtime QA autofix"], mode="fix",
        require_change=False, emit=emit)
    return list(res["changed"])


async def run_qa(
    shortid: str, *, brief: str, tech_plan: str, url: str,
    db_password: str, app_secret: str, title: str,
    emit: Callable[[dict], None], max_rounds: int = 2,
    commit_fix: Optional[Callable[[str], "asyncio.Future"]] = None,
    run_id: int = 0,
) -> dict:
    """Run the QA + autofix loop. Returns a report dict:
       {clean: bool, rounds: int, errors:[...], network:[...], server_errs:str,
        screenshot: data-uri|None, critic: str, fixes:[...]}"""
    report = {"clean": False, "rounds": 0, "errors": [], "network": [],
              "server_errs": "", "screenshot": None, "critic": "", "fixes": [],
              "semantic": [], "flows_checked": 0, "flows_passed": 0}
    flows = None          # planned once, reused across rounds (they don't change)

    for rnd in range(1, max_rounds + 1):
        report["rounds"] = rnd
        qa = await chrome.qa_run(url, exercise=True)
        server_errs = await _tail_app_logs(shortid)
        errors = qa.get("errors", [])
        network = qa.get("network", [])
        report.update({"errors": errors, "network": network,
                       "server_errs": server_errs, "screenshot": qa.get("screenshot")})

        # SEMANTIC pass (phase 28): seed a record through the app's OWN API, then assert it
        # renders. This is the only check that catches "No links yet" with rows in Postgres —
        # a caught error rendered as text leaves window.__errs empty, so console-only QA
        # passes it every time.
        emit({"type": "progress", "stage": "qa_semantic",
              "detail": f"round {rnd}: seeding a record and checking it renders"})
        sem = await semantic_qa.run(shortid, brief=brief, tech_plan=tech_plan,
                                    base_url=url, flows=flows)
        flows = sem.get("flows") or flows
        report.update({"semantic": sem.get("findings", []),
                       "flows_checked": sem.get("checked", 0),
                       "flows_passed": sem.get("passed", 0)})
        emit({"type": "qa", "round": rnd, "errors": errors[:10],
              "network": network[:10], "server_errors": bool(server_errs),
              "chrome_ok": qa.get("ok", False),
              "flows_checked": sem.get("checked", 0), "flows_passed": sem.get("passed", 0),
              "semantic": sem.get("findings", [])[:5]})

        has_problem = (bool(errors) or bool(network) or bool(server_errs)
                       or bool(sem.get("findings")))
        if not has_problem:
            report["clean"] = True
            break
        if rnd >= max_rounds:
            break  # out of budget; report honestly below

        # triage + bounded autofix
        emit({"type": "progress", "stage": "qa_autofix",
              "detail": f"round {rnd}: fixing {len(errors)} js + {len(network)} net + "
                        f"{'server' if server_errs else 'no-server'} errors + "
                        f"{len(sem.get('findings') or [])} broken flow(s)"})
        try:
            fixed = await _autofix(shortid, brief, tech_plan, errors, network,
                                   server_errs, run_id=run_id, round_no=rnd,
                                   semantic=sem.get("findings"), url=url, emit=emit)
        except Exception as e:  # noqa: BLE001
            logger.warning("autofix generation failed for %s: %s", shortid, e)
            fixed = []
        if not fixed:
            break  # couldn't produce a fix — don't loop uselessly
        report["fixes"].extend(fixed)
        # rebuild + redeploy so the next round tests the fix
        try:
            await deployer.deploy(shortid, workspace.path_for(shortid),
                                  db_password=db_password, app_secret=app_secret, title=title)
        except Exception as e:  # noqa: BLE001
            logger.warning("redeploy after autofix failed for %s: %s", shortid, e)
            report["server_errs"] = (report["server_errs"] + f"\nredeploy failed: {e}")[:4000]
            break
        if commit_fix:
            try:
                await commit_fix(f"fix: runtime QA round {rnd} ({', '.join(fixed)[:80]})")
            except Exception as e:  # noqa: BLE001
                logger.info("commit of QA fix skipped: %s", e)
        await asyncio.sleep(1.0)

    # completeness critic — which UX flow is still untested/broken?
    try:
        report["critic"] = await _critic(brief, tech_plan, report)
    except Exception as e:  # noqa: BLE001
        report["critic"] = f"(critic unavailable: {e})"
    return report


async def _critic(brief: str, tech_plan: str, report: dict) -> str:
    sys = ("You are a strict QA critic. Given the app's plan and the runtime-QA results, name "
           "any user flow from the plan that looks untested or broken. Be terse and honest — if "
           "everything looks covered, say 'All planned flows appear functional.' A flow whose "
           "seeded record did not render is BROKEN, however clean the console was.")
    user = (
        f"App brief: {brief}\n\nTechnical plan (routes/pages/flows):\n{tech_plan[:4000]}\n\n"
        f"QA result: clean={report['clean']}, js_errors={report['errors'][:8]}, "
        f"network_failures={report['network'][:8]}, "
        f"server_errors={'yes' if report['server_errs'] else 'no'}.\n"
        f"End-to-end seeded flows: {report.get('flows_passed', 0)} of "
        f"{report.get('flows_checked', 0)} passed. "
        f"Failures: {(report.get('semantic') or [])[:4]}\n\n"
        "In 1-3 sentences, what flow (if any) is still untested or broken?"
    )
    return (await gpu.chat(
        [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        temperature=0.3, num_predict=400, timeout=180,
    )).strip()
