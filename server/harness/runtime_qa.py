"""Runtime QA (phase 17) — prove the live app works in a real browser and auto-fix what doesn't.

The designer runtime-QA loop, scaled to full-stack apps:
  1. chrome-pool opens the LIVE url, exercises the UI, captures console errors + failed
     network requests + a screenshot; we also tail the <id>-app container logs for server 500s.
  2. If anything's wrong, feed errors + relevant source + a screenshot note to Kimi to localize
     the fault and produce a BOUNDED fix (full files); apply, rebuild+redeploy, re-run.
  3. Loop up to `max_rounds` (2). Stop when clean or the budget is exhausted.
  4. Completeness critic: ask which UX-doc flow is untested/broken and record it (never declare
     done prematurely — never-trust-200 for UX too).

Everything is best-effort about *capturing* but honest about *reporting*: a still-broken app is
marked live-with-warnings, never logged as success.
"""
import asyncio
import logging
import re
from typing import Callable, Dict, List, Optional

from server import chrome, deployer, gpu, workspace
from server.harness import codegen

logger = logging.getLogger(__name__)

_APP_SOURCE_FILES = ["server.js", "public/index.html", "db/migrate.js"]


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


def _read_sources(shortid: str) -> Dict[str, str]:
    files: Dict[str, str] = {}
    for rel in _APP_SOURCE_FILES:
        try:
            c = workspace.read_file_capped(shortid, rel)
            if c is not None:
                files[rel] = c
        except Exception:  # noqa: BLE001
            pass
    # include migrations (small)
    import os
    mig_dir = workspace.path_for(shortid) / "migrations"
    if mig_dir.is_dir():
        for f in sorted(os.listdir(mig_dir)):
            if f.endswith(".sql"):
                try:
                    files[f"migrations/{f}"] = (mig_dir / f).read_text("utf-8", "replace")[:6000]
                except Exception:  # noqa: BLE001
                    pass
    return files


async def _autofix(shortid: str, brief: str, tech_plan: str,
                   errors: List[str], network: List[str], server_errs: str) -> List[str]:
    """Ask Kimi to localize + fix. Applies full-file output to the workspace. Returns the list
    of files it wrote (may be empty if it couldn't produce a bounded fix)."""
    sources = _read_sources(shortid)
    problem = (
        "The live app has runtime problems. Fix them with the SMALLEST change.\n\n"
        f"Console/JS errors:\n{chr(10).join(errors[:15]) or '(none)'}\n\n"
        f"Failed network requests (status url):\n{chr(10).join(network[:15]) or '(none)'}\n\n"
        f"Server-side error log lines:\n{server_errs[:2000] or '(none)'}\n"
    )
    files = await codegen.generate_files(
        brief=brief, tech_plan=tech_plan, feature=problem,
        current_files=sources, recent_changes=["runtime QA autofix"],
        minimal=True,
    )
    written: List[str] = []
    for path, content in files.items():
        if path in sources or path.startswith(("server", "public/", "migrations/", "db/")):
            workspace.write_file(shortid, path, content)
            written.append(path)
    return written


async def run_qa(
    shortid: str, *, brief: str, tech_plan: str, url: str,
    db_password: str, app_secret: str, title: str,
    emit: Callable[[dict], None], max_rounds: int = 2,
    commit_fix: Optional[Callable[[str], "asyncio.Future"]] = None,
) -> dict:
    """Run the QA + autofix loop. Returns a report dict:
       {clean: bool, rounds: int, errors:[...], network:[...], server_errs:str,
        screenshot: data-uri|None, critic: str, fixes:[...]}"""
    report = {"clean": False, "rounds": 0, "errors": [], "network": [],
              "server_errs": "", "screenshot": None, "critic": "", "fixes": []}

    for rnd in range(1, max_rounds + 1):
        report["rounds"] = rnd
        qa = await chrome.qa_run(url, exercise=True)
        server_errs = await _tail_app_logs(shortid)
        errors = qa.get("errors", [])
        network = qa.get("network", [])
        report.update({"errors": errors, "network": network,
                       "server_errs": server_errs, "screenshot": qa.get("screenshot")})
        emit({"type": "qa", "round": rnd, "errors": errors[:10],
              "network": network[:10], "server_errors": bool(server_errs),
              "chrome_ok": qa.get("ok", False)})

        has_problem = bool(errors) or bool(network) or bool(server_errs)
        if not has_problem:
            report["clean"] = True
            break
        if rnd >= max_rounds:
            break  # out of budget; report honestly below

        # triage + bounded autofix
        emit({"type": "progress", "stage": "qa_autofix",
              "detail": f"round {rnd}: fixing {len(errors)} js + {len(network)} net + "
                        f"{'server' if server_errs else 'no-server'} errors"})
        try:
            fixed = await _autofix(shortid, brief, tech_plan, errors, network, server_errs)
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
           "everything looks covered, say 'All planned flows appear functional.'")
    user = (
        f"App brief: {brief}\n\nTechnical plan (routes/pages/flows):\n{tech_plan[:4000]}\n\n"
        f"QA result: clean={report['clean']}, js_errors={report['errors'][:8]}, "
        f"network_failures={report['network'][:8]}, "
        f"server_errors={'yes' if report['server_errs'] else 'no'}.\n\n"
        "In 1-3 sentences, what flow (if any) is still untested or broken?"
    )
    return (await gpu.chat(
        [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        temperature=0.3, num_predict=400, timeout=180,
    )).strip()
