"""The agentic codegen loop (phase 26) — replaces `generate_files`'s whole-file JSON.

One backlog feature is built by *navigating* the repo, not by re-emitting it. The model is
seeded with the feature, the technical plan and a **file list** (never file contents), then
uses the tools in `server.harness.tools` to grep/read its way to the code it needs and lands
its change with `edit_file`. Everything structural stays where it was: the pipeline still
owns deploy, health-gate, commit and the step log.

Why this exists (all observed live, all caused by whole-file JSON):

* a COMPLETE 5 KB `server.js` was misjudged as truncated and the whole 23-step build died;
* the `/health` contract was silently rewritten while the model regenerated the file;
* a migration runner was re-invented because `db/migrate.js` was never in the context;
* the frontend called an endpoint with the wrong response key because it could not look at
  that endpoint;
* per-feature cost grew with file size, because every feature re-emitted every file.

Guarantees:

* **Bounded** — `MAX_TOOL_CALLS` tool calls per attempt, then the model is forced to
  `finish`; a thrashing loop can never run away.
* **Nothing is lost** — every raw LLM response and every tool call/result is appended to the
  run's artifact JSONL *before* it is parsed or acted on (`server.harness.artifacts`).
* **Nothing broken lands** — `write_file`/`edit_file` syntax-check the RESULT and reject it
  with the parser's error, leaving the file on disk untouched.
"""
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from server import gpu
from server.harness import artifacts, tools
from server.harness.codegen import NO_SAAS_RULE

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS = int(os.environ.get("BUILDERAPPS_AGENT_MAX_TOOLS", "25"))
MAX_ATTEMPTS = int(os.environ.get("BUILDERAPPS_AGENT_ATTEMPTS", "2"))
# Kimi has a 1M context, but a transcript that grows without bound is still money and
# latency. Older tool results are elided once the history passes this many characters.
HISTORY_SOFT_CAP = 200_000

_PLATFORM = (
    "THE APP (already scaffolded, do not re-create it):\n"
    "- `server.js` — the Express entry point. `pool` is a pg Pool on process.env.DATABASE_URL, "
    "`redis` on process.env.REDIS_URL, the server listens on process.env.PORT (3000).\n"
    "- `public/index.html` — the entire self-contained frontend (inline CSS + JS, no CDN).\n"
    "- `db/migrate.js` — the platform's migration runner. It applies every `migrations/*.sql` "
    "once, in filename order, tracking them in `_migrations`, and server.js calls it on boot. "
    "You MUST NOT modify it or write another one; to add schema, create a new numbered file "
    "`migrations/NNN_slug.sql` (list_files migrations/ to see the next number).\n"
    "- `package.json` — add a dependency only if you truly need one; prefer the stdlib.\n"
)

_TOOL_DISCIPLINE = (
    "HOW TO WORK (this is a real repo, not a blank page):\n"
    "1. LOOK FIRST. Use list_files/grep/read_file to see the actual code before you touch it. "
    "Never guess what a file contains, and never assume an endpoint's response shape — read it.\n"
    "2. EDIT, DON'T REWRITE. Use edit_file for every change to an existing file: it replaces an "
    "exact, unique string. Include enough surrounding lines to make the match unique. NEVER "
    "re-emit a whole existing file — that is how untouched code silently mutates.\n"
    "3. write_file is for NEW files only (or a genuinely tiny one).\n"
    "4. Both editors syntax-check the RESULT before it lands. If you get a REJECTED error, the "
    "file was NOT changed — read the error, fix your edit, and try again.\n"
    "5. If a match fails, the error shows you the closest lines that really are in the file. "
    "Copy from those exactly (no line numbers).\n"
    "6. Make the SMALLEST change that fully implements the task. Do not refactor, rename or "
    "'improve' anything you were not asked about.\n"
    "7. When it is done — and only then — call finish with a one or two sentence summary. If "
    "the feature already exists, call finish and say so instead of duplicating it.\n"
)


def _system_prompt(mode: str) -> str:
    role = {
        "feature": "You are a meticulous full-stack engineer implementing ONE small feature at "
                   "a time in an existing Node(Express)+Postgres+Redis app.",
        "update":  "You are a meticulous full-stack engineer applying ONE requested change to a "
                   "live Node(Express)+Postgres+Redis app. Keep the diff minimal.",
        "fix":     "You are a senior engineer debugging a live Node(Express)+Postgres+Redis app. "
                   "Find the actual cause (read the code, tail app_logs) and fix it minimally.",
    }.get(mode, "You are a meticulous full-stack engineer working in an existing "
                "Node(Express)+Postgres+Redis app.")
    return f"{role}\n\n{_TOOL_DISCIPLINE}\n{_PLATFORM}\n{NO_SAAS_RULE}"


def _seed(*, brief: str, tech_plan: str, task: str, overview: str,
          recent_changes: List[str], extra: str, mode: str) -> str:
    changes = "\n".join(f"- {c}" for c in (recent_changes or [])[-8:]) or "(none yet)"
    parts = [
        f"App brief: {brief}",
        f"\nTechnical plan (reference):\n{(tech_plan or '')[:5000]}",
        f"\nRecent changes:\n{changes}",
        f"\nProject files (names only — read what you need):\n{overview}",
    ]
    if extra:
        parts.append(f"\n{extra}")
    label = {"feature": "FEATURE TO BUILD NOW", "update": "CHANGE REQUEST",
             "fix": "PROBLEM TO FIX"}.get(mode, "TASK")
    parts.append(f"\n{label}: {task}\n\nStart by looking at the code you will need to change.")
    return "\n".join(parts)


def _trim(messages: List[Dict[str, Any]]) -> None:
    """Elide the oldest tool results once the history gets long (keeps the last 8 intact)."""
    size = sum(len(str(m.get("content") or "")) for m in messages)
    if size <= HISTORY_SOFT_CAP:
        return
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idx[:-8]:
        c = messages[i].get("content") or ""
        if len(c) > 400 and not c.startswith("[elided"):
            messages[i]["content"] = f"[elided {len(c)} chars of earlier tool output]"
            size -= len(c)
            if size <= HISTORY_SOFT_CAP:
                return


async def _one_attempt(*, project_id: str, run_id: int, step: str, attempt: int,
                       brief: str, tech_plan: str, task: str, recent_changes: List[str],
                       extra: str, mode: str,
                       emit: Optional[Callable[[dict], None]]) -> Dict[str, Any]:
    box = tools.Toolbox(project_id)
    rec = artifacts.Recorder(project_id, run_id, step, attempt)
    overview = box.list_files("")
    system = _system_prompt(mode)
    seed = _seed(brief=brief, tech_plan=tech_plan, task=task, overview=overview,
                 recent_changes=recent_changes, extra=extra, mode=mode)
    rec.write("seed", mode=mode, step=step, attempt=attempt, task=task,
              system=system, user=seed)

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": seed},
    ]
    nudges = 0
    llm_chars = 0
    turns = 0

    while box.calls < MAX_TOOL_CALLS and not box.finished:
        _trim(messages)
        try:
            reply = await gpu.chat_tools(messages, tools.TOOL_SCHEMAS,
                                         temperature=0.2, num_predict=8000, timeout=300)
        except Exception as e:  # noqa: BLE001
            rec.write("error", where="chat_tools", error=str(e))
            raise
        turns += 1
        # PERSIST BEFORE PARSING — a corrupt/unusable reply must still be recoverable.
        rec.write("llm_response", turn=turns, content=reply.get("content", ""),
                  tool_calls=reply.get("tool_calls"),
                  finish_reason=reply.get("finish_reason"), usage=reply.get("usage"))
        llm_chars += len(reply.get("content") or "")
        for c in reply.get("tool_calls") or []:
            a = c.get("arguments")
            llm_chars += len(a if isinstance(a, str) else str(a))

        messages.append(gpu.assistant_tool_message(reply))
        calls = reply.get("tool_calls") or []
        if not calls:
            nudges += 1
            if nudges > 2:
                rec.write("end", reason="model stopped calling tools")
                break
            messages.append({"role": "user", "content":
                             "You must either use a tool to make the change or call finish. "
                             "Do not describe the change in prose — apply it with edit_file "
                             "or write_file."})
            continue

        for call in calls:
            name, args = call["name"], call["arguments"]
            result = await box.call(name, args)
            rec.write("tool_call", turn=turns, tool=name, arguments=args, result=result)
            if emit and name in ("write_file", "edit_file"):
                emit({"type": "progress", "stage": "codegen",
                      "detail": f"{name}: {result[:120]}"})
            messages.append(gpu.tool_result_message(call["id"], name, result))
            if box.finished:
                break

    if not box.finished and box.calls >= MAX_TOOL_CALLS:
        # Budget exhausted: force a finish so we always get a summary of what it did.
        messages.append({"role": "user", "content":
                         f"Tool budget reached ({MAX_TOOL_CALLS} calls). Call finish now with "
                         "a summary of what you changed and what is still missing."})
        try:
            reply = await gpu.chat_tools(
                messages, tools.TOOL_SCHEMAS, temperature=0.2, num_predict=1000, timeout=180,
                tool_choice={"type": "function", "function": {"name": "finish"}})
            rec.write("llm_response", turn=turns + 1, forced_finish=True,
                      content=reply.get("content", ""), tool_calls=reply.get("tool_calls"))
            for call in reply.get("tool_calls") or []:
                if call["name"] == "finish":
                    await box.call("finish", call["arguments"])
        except Exception as e:  # noqa: BLE001 — a failed forced finish is not fatal
            rec.write("error", where="forced_finish", error=str(e))

    out = {
        "changed": sorted(box.changed.keys()),
        "actions": dict(box.changed),
        "summary": box.summary or (f"{len(box.changed)} file(s) changed"),
        "tool_calls": box.calls,
        "turns": turns,
        "llm_output_chars": llm_chars,
        "finished": box.finished,
        "artifact": str(rec),
    }
    rec.write("end", **{k: v for k, v in out.items() if k != "artifact"})
    return out


async def run_agent(*, project_id: str, run_id: int, step: str, brief: str, tech_plan: str,
                    task: str, recent_changes: Optional[List[str]] = None,
                    extra: str = "", mode: str = "feature",
                    require_change: bool = True,
                    emit: Optional[Callable[[dict], None]] = None) -> Dict[str, Any]:
    """Run the agentic loop for one task. Returns the `_one_attempt` result dict.

    `require_change=True` retries once (a fresh attempt, its own artifact file) if the agent
    ended without touching a single file *and* without deciding it was already done — that
    combination means it thrashed, and a second pass with the failure noted usually lands it.
    """
    artifacts.prune(project_id)
    last: Dict[str, Any] = {}
    extra_now = extra
    for attempt in range(MAX_ATTEMPTS):
        last = await _one_attempt(
            project_id=project_id, run_id=run_id, step=step, attempt=attempt,
            brief=brief, tech_plan=tech_plan, task=task,
            recent_changes=recent_changes or [], extra=extra_now, mode=mode, emit=emit)
        if last["changed"] or last["finished"] or not require_change:
            break
        logger.warning("agent attempt %d for '%s' changed nothing; retrying", attempt, step)
        extra_now = (extra + "\n\nNOTE: a previous attempt ran out of tool calls without "
                     "changing any file. Do not over-explore — read only what you need, then "
                     "make the edit.").strip()
    if require_change and not last.get("changed") and not last.get("finished"):
        raise RuntimeError(
            f"agentic codegen changed nothing for '{task[:80]}' after {MAX_ATTEMPTS} attempts "
            f"(transcript: {last.get('artifact')})")
    return last
