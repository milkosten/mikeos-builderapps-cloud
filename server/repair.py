"""Phase 31 — a failed deploy tells the assistant what broke, in the open, and bounded.

Before this, a deploy that went red died in a `deployments` row. The assistant that pushed the
commit was never told; it was **write-only**, and every broken commit needed a human. This
module is the return path.

## The shape of it

    deployer raises DeployFailure(stage, summary, envelope)
        -> shipper hands the envelope here
            -> `@Developer` message written into the /builder thread  (the user sees it)
            -> the SAME text dispatched as the next beat's task       (the agent acts on it)

Two deliveries of one fact, deliberately. A private callback would let the assistant quietly
fix its own mistakes, and **a silent self-heal is indistinguishable from a silent failure** —
this codebase has been bitten by things that "succeeded" invisibly more than once. The user
must be able to read what broke, what the assistant was told, and what it did about it.

The beat is seeded in the normal grounding order — docs -> SOUL -> repo state + recent beats ->
**the failure envelope last**, as the direct instruction. That is exactly the path a human's
`@Developer fix the header` takes (`assistant_beats.user_ask`), so a repair beat is grounded
like any other beat rather than through a special back door.

## The bounds, which are the actually hard part

An agent handed its own failure and told to fix it will try forever, and every try costs money
and a build slot. So:

  * **2 repair attempts per episode**, then stop. An *episode* — not a commit — carries the
    budget, because each repair pushes a new sha and a per-commit cap would never bind.
  * **an identical failure twice stops it immediately.** Same stage, same first error line
    means nothing the agent did changed anything; retrying that is superstition, not debugging.
  * **fix forward.** The bad commit stays in the log. Nothing here rewrites history.
  * **no deploy storm.** The repair beat is dispatched only once the failed run is terminal,
    and `act_request_deploy` still refuses to start while another run is in flight.
  * **escalation is a stop, not a slow down**: project `needs_attention`, a plain-language
    message in the thread, and the assistant PAUSED so it is not left beating against a wall.

## What blue/green changed here

The narration used to have to say "rolled back to 9b21d40 — the app is still up". It now says
something better and simpler: the new colour never took traffic, so **the live app was never
touched at all**. The repair beat is not racing a down app, and there is no revert commit
fighting the fix the assistant is about to write.
"""
import asyncio
import logging
import re
from typing import Optional

from server import store

logger = logging.getLogger(__name__)

MAX_REPAIR_ATTEMPTS = 2

# How much of the envelope the beat is given. The thread message is separately clamped to the
# thread's own 4000-char entry limit — the human gets the headline and the real error text, the
# agent gets the full tail.
MAX_TASK_CHARS = 9000

_STAGE_WORDS = {
    "build": "the image would not build",
    "up": "the container would not start",
    "health_gate": "the health gate",
    "public_check": "the public route check",
}

# A line that is actually about the failure, as opposed to the fifty lines of banner around it.
#
# Tried in PRIORITY order, not in the order they appear in the log. A node crash prints
# `throw err;` three lines ABOVE `Error: Cannot find module './lib/nope'`, so "the first line
# that looks like an error" reliably picks the least informative one in the dump. What we want
# is the most specific match anywhere in the text.
_ERROR_LINE_PATTERNS = [
    re.compile(p, re.I | re.M) for p in (
        r"^.*Cannot find module.*$",
        r"^.*\bE(?:ACCES|CONNREFUSED|CONNRESET|NOENT|ADDRINUSE|PERM|NOTFOUND|AI_AGAIN)\b.*$",
        r"^.*\b[0-9][0-9A-Z]{4}\b.*(?:password|permission|denied|does not exist|violat|"
        r"syntax error).*$",                      # Postgres SQLSTATE lines, e.g. 28P01
        r"^\s*(?:Uncaught\s+)?(?:Type|Range|Reference|Syntax|Eval|URI|Assertion)?Error:.*$",
        r"^\s*npm ERR!.*$",
        r"^.*\bfailed to\b.*$",
        r"^.*\berror\b.*$",
        r"^\s*throw\b.*$",
    )
]


def first_error_line(envelope: dict) -> str:
    """The one line that names the bug — the thing a human would have grepped for.

    Used for two different jobs and it has to be good at both: it is the headline of the
    message a person reads, and it is the SIGNATURE that decides whether this failure is the
    same one as last time. A signature computed from the whole log would never match twice
    (timestamps, ports, pids), and one computed from the stage alone would always match.
    """
    for field in ("app_logs", "build_log", "docker_error"):
        text = str(envelope.get(field) or "")
        if not text.strip():
            continue
        for pattern in _ERROR_LINE_PATTERNS:
            m = pattern.search(text)
            if m:
                return " ".join(m.group(0).split())[:300]
    health = envelope.get("health") or {}
    if isinstance(health, dict) and health.get("http"):
        return f"public /health answered HTTP {health.get('http')}"
    for field in ("app_logs", "build_log", "docker_error"):
        lines = [ln.strip() for ln in str(envelope.get(field) or "").splitlines() if ln.strip()]
        if lines:
            return lines[-1][:300]
    return str(envelope.get("summary") or "")[:300]


def signature_for(envelope: dict) -> str:
    """stage + the error line, with the volatile parts removed.

    Digits are collapsed because a pid, a port, a timestamp or a line offset changing does not
    make it a different bug — and if we let them count, "identical failure twice" would never
    fire and the cap would be the only bound left.
    """
    line = re.sub(r"\d+", "#", first_error_line(envelope).lower())
    return f"{envelope.get('stage', '')}|{line}"[:400]


def _evidence_block(envelope: dict) -> str:
    parts: list[str] = []
    if envelope.get("build_log"):
        parts.append("BUILD LOG (tail):\n" + str(envelope["build_log"]))
    if envelope.get("docker_error"):
        parts.append("DOCKER SAID:\n" + str(envelope["docker_error"]))
    if envelope.get("app_logs"):
        parts.append(f"CONTAINER LOGS ({envelope.get('colour', '')} app, tail):\n"
                     + str(envelope["app_logs"]))
    health = envelope.get("health")
    if isinstance(health, dict) and health:
        parts.append(f"PUBLIC CHECK: HTTP {health.get('http')} from {health.get('url')}\n"
                     + str(health.get("body") or ""))
    return "\n\n".join(parts)


def task_text(envelope: dict, *, attempt: int) -> str:
    """The instruction the repair beat is given. Written as a ticket, not as a log dump —
    the agent gets the evidence, but it also gets told exactly what is expected of it."""
    stage = str(envelope.get("stage") or "")
    commit = str(envelope.get("commit") or "")[:12]
    head = (
        f"The deploy of your commit {commit} FAILED at {_STAGE_WORDS.get(stage, stage)}.\n"
        f"{envelope.get('summary', '')}\n\n"
        + ("The live app was NOT affected: the new container was health-gated before it was "
           "given any traffic, so the previous version is still serving users normally. "
           "Nothing is on fire — but the change you pushed is not live and will not be until "
           "it builds and passes the gate.\n\n"
           if envelope.get("live_unaffected") else
           "This project has no previously-working version serving, so the app is currently "
           "not up.\n\n")
        + f"THE ERROR:\n    {first_error_line(envelope)}\n\n"
    )
    tail = (
        "\n\nWHAT TO DO NOW:\n"
        "- Read the error above and find its actual cause in the repository.\n"
        "- Fix it FORWARD: make a new commit that repairs the problem. Do NOT revert, do NOT "
        "rewrite history, and do NOT undo the feature you were building unless the feature "
        "itself is the problem.\n"
        "- Then request a deploy again so the fix is built and health-gated.\n"
        f"- This is repair attempt {attempt} of {MAX_REPAIR_ATTEMPTS}. If it fails again with "
        "the same error, the platform stops and hands this to the human owner — so do not "
        "repeat a change that has already failed; find a different cause.\n"
    )
    room = MAX_TASK_CHARS - len(head) - len(tail)
    evidence = _evidence_block(envelope)
    if len(evidence) > room:
        evidence = "…(truncated)…\n" + evidence[-max(room, 0):]
    return head + evidence + tail


def thread_message(name: str, envelope: dict, *, verdict: str, attempt: int = 0) -> str:
    """The `@message` the OWNER reads in the left pane. Same fact, human-sized.

    Capped well under the thread's 4000-char entry limit and built to be readable at a glance:
    what was being deployed, what stage failed, whether anything is broken for users, the real
    error text, and what happens next. `store.append_message` clamps anyway; clamping here
    means the cut lands somewhere chosen rather than mid-word in a stack trace.
    """
    stage = str(envelope.get("stage") or "")
    commit = str(envelope.get("commit") or "")[:8]
    safe = ("the live app is untouched — the new container never took traffic"
            if envelope.get("live_unaffected") else
            "this project has no previously-working version, so it is currently down")
    body = (
        f"🚀 **{name}** — deploy of `{commit}` FAILED at {_STAGE_WORDS.get(stage, stage)}.\n"
        f"↩ {safe}.\n\n"
        f"@{name} deploy of {commit} failed:\n\n"
        f"```\n{first_error_line(envelope)}\n```\n"
    )
    detail = _evidence_block(envelope)
    room = 3800 - len(body) - len(verdict) - 60
    if detail and room > 200:
        if len(detail) > room:
            detail = "…(truncated)…\n" + detail[-room:]
        # Plain text delimiters, not `<details>`: the thread bubble renders text content, so
        # an HTML tag arrives as the literal characters "<details>" on the user's screen. That
        # is correct escaping (agent output must never be injected as markup) and therefore
        # the message has to be written for a renderer that shows exactly what it is given.
        body += f"\n— evidence —\n{detail}\n— end of evidence —\n"
    return (body + "\n" + verdict)[:4000]


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------
async def on_deploy_failed(project_id: str, envelope: dict, *,
                           assistant_id: Optional[int] = None,
                           beat_id: Optional[int] = None,
                           run_id: Optional[int] = None) -> dict:
    """One red deploy: record it, tell the thread, and either dispatch a repair or escalate.

    Never raises. A failure in the failure path would replace a real, reported error with a
    control-plane traceback — the single worst outcome for the person trying to understand
    what happened to their app.
    """
    try:
        return await _on_deploy_failed(project_id, envelope, assistant_id=assistant_id,
                                       beat_id=beat_id, run_id=run_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("phase-31 feedback failed for %s", project_id)
        return {"delivered": False, "detail": str(e)[:300]}


async def _on_deploy_failed(project_id: str, envelope: dict, *,
                            assistant_id: Optional[int], beat_id: Optional[int],
                            run_id: Optional[int]) -> dict:
    from server import assistants as A

    stage = str(envelope.get("stage") or "unknown")
    sig = signature_for(envelope)
    decision = await store.record_repair_failure(
        project_id, assistant_id=assistant_id, failed_sha=str(envelope.get("commit") or ""),
        stage=stage, signature=sig)

    assistant = await A.get(int(assistant_id), project_id) if assistant_id else None
    name = (assistant or {}).get("name") or (assistant or {}).get("role") or "Developer"

    # ---- the bounds, applied BEFORE anything is dispatched -----------------
    stop_reason = ""
    if not assistant:
        stop_reason = ("this deploy was not requested by an assistant, so there is nobody to "
                       "hand the error to")
    elif decision["repeat"]:
        stop_reason = ("the deploy failed with the SAME error as the previous attempt — "
                       "repeating it would not tell us anything new")
    elif decision["attempts"] >= MAX_REPAIR_ATTEMPTS:
        stop_reason = (f"{decision['attempts']} repair attempts have already been made on this "
                       f"failure and it is still red")

    if stop_reason:
        verdict = await _escalate(project_id, assistant, envelope, decision, stop_reason)
        await store.append_message(
            project_id, "system",
            thread_message(name, envelope, verdict=verdict),
            meta={"kind": "deploy_failed", "assistant_id": assistant_id, "beat_id": beat_id,
                  "run_id": run_id, "stage": stage, "sha": envelope.get("commit"),
                  "escalated": True, "attempts": decision["attempts"]})
        return {"delivered": True, "escalated": True, "attempts": decision["attempts"]}

    attempt = await store.count_repair_attempt(decision["episode_id"])
    verdict = (f"I've handed the full error to **{name}** as its next task "
               f"(repair attempt {attempt} of {MAX_REPAIR_ATTEMPTS}). "
               f"It will fix forward and redeploy; if it fails again with the same error I "
               f"stop and ask you.")
    await store.append_message(
        project_id, "system", thread_message(name, envelope, verdict=verdict, attempt=attempt),
        meta={"kind": "deploy_failed", "assistant_id": assistant_id, "beat_id": beat_id,
              "run_id": run_id, "stage": stage, "sha": envelope.get("commit"),
              "attempt": attempt})

    asyncio.create_task(_dispatch_repair_beat(
        int(assistant["id"]), project_id, task_text(envelope, attempt=attempt), run_id))
    return {"delivered": True, "escalated": False, "attempt": attempt}


async def on_deploy_healthy(project_id: str) -> None:
    """A green deploy closes the episode — the budget is per FAILURE RUN, not per lifetime."""
    try:
        if await store.close_repair(project_id, "resolved", "a later deploy went green"):
            logger.info("repair episode for %s resolved by a green deploy", project_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("could not close repair episode for %s: %s", project_id, e)


async def _escalate(project_id: str, assistant: Optional[dict], envelope: dict,
                    decision: dict, why: str) -> str:
    """Stop. Pause the assistant, flag the project, and say plainly what was tried.

    Pausing matters more than it looks: an assistant left `active` after an escalation goes on
    beating on its schedule, and the next beat sees a repo whose HEAD does not deploy. Better
    a stopped agent and a clear question than a busy agent and a growing bill.
    """
    await store.close_repair(project_id, "escalated", f"{why}; stage={envelope.get('stage')}")
    try:
        await store.set_project_status(project_id, "needs_attention")
    except Exception as e:  # noqa: BLE001
        logger.warning("could not flag %s as needs_attention: %s", project_id, e)
    if assistant:
        from server import assistants as A
        try:
            await A.set_status(int(assistant["id"]), project_id, "paused")
        except Exception as e:  # noqa: BLE001
            logger.warning("could not pause assistant %s: %s", assistant.get("id"), e)
    live = envelope.get("live_unaffected")
    return (
        "**I could not fix this — over to you.**\n"
        f"{why}. I have stopped instead of trying again.\n"
        + (f"- Attempts made: {decision['attempts']} of {MAX_REPAIR_ATTEMPTS}\n"
           f"- First failing commit: `{str(decision.get('origin_sha') or '')[:8]}`\n"
           f"- Still failing at: {envelope.get('stage')}\n")
        + ("- Your app is still running the last version that worked, so users are not "
           "affected.\n" if live else "- The app is not currently serving.\n")
        + (f"- **{assistant.get('name') or 'The assistant'}** is paused; start it again once "
           "you have looked, or tell it what to do differently.\n" if assistant else ""))


async def _dispatch_repair_beat(assistant_id: int, project_id: str, task: str,
                                run_id: Optional[int]) -> None:
    """Give the assistant its next beat, carrying the failure as the task.

    Waits for the failed run to actually finish first. Not politeness: `act_request_deploy`
    refuses to start while a run is in flight, so a repair beat that began too early would
    read the error, write a fix, ask to deploy it and be told "busy" — a wasted beat that
    looks from the outside exactly like an agent that ignored the instruction.
    """
    from server import assistant_runtime as R
    from server import assistants as A
    from server import runner

    try:
        for _ in range(90):                       # ~3 min ceiling, then go anyway
            if run_id and runner.is_active(int(run_id)):
                await asyncio.sleep(2)
                continue
            proj = await store.get_project(project_id) or {}
            if proj.get("status") in ("creating", "building", "deploying"):
                await asyncio.sleep(2)
                continue
            break

        assistant = await A.get(assistant_id, project_id)
        if not assistant:
            logger.warning("repair beat: assistant %s is gone", assistant_id)
            return
        # any_status: the assistant may be paused (a human paused it, or an earlier
        # escalation did). A deploy IT caused is still its problem to hear about — but a
        # paused agent will not schedule itself another beat afterwards, which is the point.
        claimed = await A.claim(assistant_id, runner.INSTANCE_ID, any_status=True)
        if not claimed:
            logger.warning("repair beat for %s: a beat is already in flight, not queuing a "
                           "second one", assistant_id)
            return
        beat_id = await A.start_beat(assistant_id, project_id, "repair", task)
        logger.warning("repair beat %s dispatched to assistant %s for %s",
                       beat_id, assistant_id, project_id)
        await R.execute_beat(claimed, beat_id, "repair")
    except Exception:  # noqa: BLE001
        logger.exception("repair beat dispatch failed for assistant %s", assistant_id)
