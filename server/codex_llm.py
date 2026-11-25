"""Chat-completion style calls on Codex (Mike's flat-rate ChatGPT plan) — the builderapps LLM backend.

Mike's standing rule: flat-rate subscriptions only (Codex / ZAI), never a pay-per-request key.
Until 2026-11-25 every call here went to OpenRouter (Kimi k3, per-token billing; the key had been
dead since 2026-09-08, so every build and assistant beat failed). Now each call is one stateless
`codex exec` in a throwaway jail container with NO workspace mounted (`server/codex_runner.py`):
the harness keeps owning its tools, its transcripts and its syntax gates exactly as before.

Three shapes, matching `server.gpu`:
  * `text()`      — messages -> assistant text (optionally a JSON object).
  * `tool_turn()` — messages + OpenAI-style tool schemas -> ONE assistant turn that either calls
                    tools or answers. Codex has its own tools, so the external ones are emulated:
                    the model returns {content, tool_calls[{name, arguments_json}]} under a strict
                    `--output-schema`, and we hand back the OpenAI shape the harness expects.
  * cost is 0 (flat rate); token counts still go to the Usage tab and to fleet telemetry.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List, Optional

from server import codex_runner

TEXT_EFFORT = os.environ.get("BUILDERAPPS_CODEX_EFFORT", "low").strip() or None
TOOL_EFFORT = os.environ.get("BUILDERAPPS_CODEX_TOOL_EFFORT", "low").strip() or None
SOURCE_ID = "builderapps"
MODEL_LABEL = "codex"

_NO_SHELL = ("You are being used as a plain language model behind an API. Your working directory "
             "is empty and you must NOT run any shell command, read files or browse: answer from "
             "the conversation below only.")


def _content_text(content: Any) -> str:
    """OpenAI message content: a string, or a list of parts ({type:text,text}, images...)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in ("text", "input_text", "output_text"):
                    out.append(str(part.get("text") or ""))
                elif part.get("type") in ("image_url", "input_image"):
                    out.append("[image omitted]")
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(out)
    return str(content)


def render(messages: List[Dict[str, Any]]) -> str:
    """Flatten an OpenAI-style message list into one prompt (system first, then the turns)."""
    system = [m for m in messages if m.get("role") in ("system", "developer")]
    turns = [m for m in messages if m.get("role") not in ("system", "developer")]
    parts: List[str] = []
    if system:
        parts.append("# Instructions\n\n" + "\n\n".join(_content_text(m.get("content"))
                                                       for m in system))
    parts.append("# Conversation")
    for m in turns:
        role = m.get("role")
        if role == "user":
            parts.append("## USER\n" + _content_text(m.get("content")))
        elif role == "assistant":
            txt = _content_text(m.get("content"))
            calls = m.get("tool_calls") or []
            block = "## ASSISTANT\n" + (txt or "")
            for c in calls:
                fn = (c or {}).get("function") or {}
                block += (f"\n[called tool `{fn.get('name')}` id={c.get('id')} "
                          f"arguments={fn.get('arguments')}]")
            parts.append(block)
        elif role == "tool":
            parts.append(f"## TOOL RESULT ({m.get('name') or 'tool'}, id={m.get('tool_call_id')})\n"
                         + _content_text(m.get("content")))
        else:
            parts.append(f"## {str(role).upper()}\n" + _content_text(m.get("content")))
    return "\n\n".join(parts)


def _usage_openai(u: Dict[str, int]) -> Dict[str, Any]:
    """Codex usage -> the OpenAI-ish block server.gpu's accounting understands (cost 0)."""
    return {"prompt_tokens": int(u.get("input_tokens") or 0),
            "completion_tokens": int(u.get("output_tokens") or 0),
            "prompt_tokens_details": {"cached_tokens": int(u.get("cached_input_tokens") or 0)},
            "completion_tokens_details": {
                "reasoning_tokens": int(u.get("reasoning_output_tokens") or 0)},
            "cost": 0}


async def text(messages: List[Dict[str, Any]], *, schema: Optional[Dict[str, Any]] = None,
               timeout: float = 600.0) -> Dict[str, Any]:
    """-> {"content": str, "usage": openai-usage}. Raises RuntimeError on a failed run."""
    prompt = _NO_SHELL + "\n\n" + render(messages)
    if schema is not None:
        prompt += ("\n\n# Output format\nReply with ONE JSON object and nothing else (no prose, no "
                   "code fences). It must satisfy this JSON schema:\n" + json.dumps(schema))
    res = await codex_runner.run(prompt, timeout=timeout, effort=TEXT_EFFORT, source_id=SOURCE_ID)
    if not res.ok:
        raise RuntimeError(f"codex call failed: {res.error[:400]}")
    content = res.text
    if schema is not None:
        try:
            content = json.dumps(codex_runner.parse_json(content))
        except Exception:  # noqa: BLE001 — let the caller's own JSON repair loop see it
            pass
    return {"content": content, "usage": _usage_openai(res.usage)}


def _tool_schema(names: List[str]) -> Dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "required": ["content", "tool_calls"],
            "properties": {
                "content": {"type": "string"},
                "tool_calls": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["name", "arguments_json"],
                    "properties": {"name": {"type": "string", "enum": names},
                                   "arguments_json": {"type": "string"}}}}}}


async def tool_turn(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], *,
                    tool_choice: Any = None, timeout: float = 600.0) -> Dict[str, Any]:
    """ONE tool-enabled assistant turn, in server.gpu.chat_tools' normalized shape."""
    specs = []
    names: List[str] = []
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        if fn.get("name"):
            names.append(fn["name"])
            specs.append({"name": fn["name"], "description": fn.get("description", ""),
                          "parameters": fn.get("parameters", {})})
    forced = None
    if isinstance(tool_choice, dict):
        forced = ((tool_choice.get("function") or {}).get("name")) or None
    elif tool_choice == "required":
        forced = "any"
    rules = (
        "# How you act\n"
        "You are the decision step of an agent whose tools are run by the HARNESS, not by you. "
        "Do NOT run shell commands or read files yourself (your directory is empty). Each turn, "
        "either call one or more of the tools below (put them in `tool_calls`; `arguments_json` "
        "is a JSON object string matching that tool's parameters; independent calls can go in "
        "one turn), or answer with `content` and an EMPTY `tool_calls` when no tool is needed. "
        "Tool results arrive as TOOL RESULT turns in the conversation.\n\n"
        "# Tools\n" + json.dumps(specs, ensure_ascii=False))
    if forced == "any":
        rules += "\n\nYou MUST call at least one tool this turn."
    elif forced:
        rules += f"\n\nYou MUST call the tool `{forced}` this turn."
    prompt = render(messages) + "\n\n" + rules
    allowed = [forced] if forced and forced != "any" and forced in names else (names or ["none"])
    res = await codex_runner.run(prompt, timeout=timeout, effort=TOOL_EFFORT,
                                 schema=_tool_schema(allowed), source_id=SOURCE_ID)
    if not res.ok:
        raise RuntimeError(f"codex tool call failed: {res.error[:400]}")
    try:
        data = codex_runner.parse_json(res.text)
    except Exception:  # noqa: BLE001 — treat an unparseable turn as plain content
        data = {"content": res.text, "tool_calls": []}
    content = str(data.get("content") or "")
    calls = []
    raw_calls = []
    for i, c in enumerate(data.get("tool_calls") or []):
        name = (c or {}).get("name")
        if not name or name not in allowed:
            continue
        args = (c or {}).get("arguments_json") or "{}"
        cid = f"call_{uuid.uuid4().hex[:12]}"
        calls.append({"id": cid, "name": name, "arguments": args})
        raw_calls.append({"id": cid, "type": "function",
                          "function": {"name": name, "arguments": args}})
    raw: Dict[str, Any] = {"role": "assistant", "content": content}
    if raw_calls:
        raw["tool_calls"] = raw_calls
    return {"content": content, "tool_calls": calls,
            "finish_reason": "tool_calls" if calls else "stop", "raw": raw,
            "usage": _usage_openai(res.usage)}
