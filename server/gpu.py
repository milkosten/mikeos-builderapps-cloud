"""LLM client for the designer harness.

Two backends, chosen at runtime:

* **OpenRouter (preferred when `OPENROUTER_API_KEY` is set)** — used for the *designer
  project only*. Routes to `BUILDERAPPS_LLM_MODEL` (default `moonshotai/kimi-k3`, a strong
  reasoning model with a 1M context) via the OpenAI-compatible chat/completions API.
* **Ollama fallback** — the shared free `qwen3:8b` at `OLLAMA_GPU_URL` (self-signed,
  HTTP-Basic, `think:false`, serialized to one in-flight call).

`chat()` has the same signature for both, so the rest of the harness is unchanged.
The OpenRouter key is scoped to this service's env only — never committed, never shared.
"""
import asyncio
import base64
import json
import logging
import os
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# --- OpenRouter (Kimi) -----------------------------------------------------
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
LLM_MODEL = os.environ.get("BUILDERAPPS_LLM_MODEL", "moonshotai/kimi-k3").strip()
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# --- Ollama fallback -------------------------------------------------------
OLLAMA_GPU_URL = os.environ.get(
    "OLLAMA_GPU_URL",
    "ollama://mikeos:uB49VXwMDy7R2JE0H7mI@81.8.177.182:11443",
)
TEXT_MODEL = os.environ.get("OLLAMA_TEXT_MODEL", "qwen3:8b")

# Only ONE shared-GPU request in flight at a time (Ollama path only).
_gpu_sem = asyncio.Semaphore(1)


def backend() -> str:
    return f"openrouter:{LLM_MODEL}" if OPENROUTER_API_KEY else f"ollama:{TEXT_MODEL}"


def _endpoint() -> tuple[str, Dict[str, str]]:
    """Parse OLLAMA_GPU_URL (ollama://user:pass@host:port) -> (base_url, headers)."""
    raw = OLLAMA_GPU_URL
    scheme = "https"
    rest = raw
    if "://" in raw:
        s, rest = raw.split("://", 1)
        s = s.lower()
        scheme = "http" if s in ("ollama+http", "http") else "https"
    headers: Dict[str, str] = {}
    if "@" in rest:
        creds, rest = rest.rsplit("@", 1)
        headers["Authorization"] = "Basic " + base64.b64encode(creds.encode()).decode()
    return f"{scheme}://{rest.rstrip('/')}", headers


# ---- token / cost accounting -------------------------------------------------------
# Every LLM call reports what it consumed. The harness installs a sink (see
# server.usage) that stamps the current project/run/step onto each record, so the
# builder's Usage tab can show input / output / cached tokens and cost per project.
#
# OpenRouter returns REAL cost when we send {"usage":{"include":true}} — we use that
# whenever it is present and only fall back to the rate table below when it is not.
# Rates are USD per 1M tokens and are overridable without a redeploy.
KIMI_IN_PER_M = float(os.environ.get("LLM_PRICE_IN_PER_M", "0.60"))
KIMI_OUT_PER_M = float(os.environ.get("LLM_PRICE_OUT_PER_M", "2.50"))
KIMI_CACHED_IN_PER_M = float(os.environ.get("LLM_PRICE_CACHED_IN_PER_M", "0.15"))

_usage_sink: Optional[Any] = None


def set_usage_sink(fn) -> None:
    """Install a callable(record: dict) invoked after every LLM call. Best-effort."""
    global _usage_sink
    _usage_sink = fn


def _report_usage(usage: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Normalise a provider usage block, cost it, and hand it to the sink."""
    try:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or usage.get("cached_tokens") or 0)
        fresh_in = max(0, prompt - cached)
        cost = usage.get("cost")
        estimated = cost is None
        if estimated:  # provider didn't tell us — price it from the rate table
            cost = (fresh_in * KIMI_IN_PER_M + cached * KIMI_CACHED_IN_PER_M
                    + completion * KIMI_OUT_PER_M) / 1_000_000.0
        rec = {"model": model, "prompt_tokens": prompt, "completion_tokens": completion,
               "cached_tokens": cached, "cost_usd": round(float(cost), 6),
               "cost_estimated": estimated}
        if _usage_sink:
            try:
                _usage_sink(rec)
            except Exception:  # accounting must NEVER break a build
                logger.debug("usage sink failed", exc_info=True)
        return {**usage, "_accounted": rec}
    except Exception:
        logger.debug("usage parse failed", exc_info=True)
        return usage


async def _openrouter_chat(messages: List[Dict[str, Any]], schema: Optional[Dict[str, Any]],
                           temperature: float, num_predict: int, timeout: float,
                           max_retries: int) -> str:
    """One OpenRouter (Kimi) call. Kimi-k3 is a reasoning model, so give generous
    max_tokens (reasoning tokens count toward the completion budget) — small steps like
    the classifier are bounded lower to save cost."""
    or_max = 2000 if num_predict <= 1024 else max(int(num_predict), 12000)
    body: Dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": or_max,
        # Skip the model's chain-of-thought — it ~doubles latency and our deterministic
        # linter/autofix/token-enforcement already guarantee correctness. Route to the
        # fastest provider.
        "reasoning": {"enabled": False},
        "provider": {"sort": "throughput"},
        # Ask OpenRouter to return real accounting (cost + cached/prompt/completion tokens)
        # so the Usage tab reports what we ACTUALLY paid rather than an estimate.
        "usage": {"include": True},
    }
    if schema is not None:
        body["response_format"] = {"type": "json_object"}  # ask for valid JSON
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://builderapps.osmike.com",
        "X-Title": "MikeOS builderapps",
    }
    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(OPENROUTER_URL, json=body, headers=headers)
            if resp.status_code in (408, 409, 429, 500, 502, 503, 520, 524, 529):
                wait = min(40.0, 4.0 * (2 ** attempt))
                logger.warning("OpenRouter %s, retry in %.0fs", resp.status_code, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            _report_usage(data.get("usage") or {}, data.get("model") or LLM_MODEL)
            msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
            content = msg.get("content") or ""
            if not content.strip():
                last_err = RuntimeError("OpenRouter returned empty content")
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            return content
        except httpx.HTTPStatusError as e:
            last_err = e
            logger.warning("OpenRouter HTTP %s (attempt %d)", e.response.status_code, attempt + 1)
            await asyncio.sleep(min(20.0, 3.0 * (2 ** attempt)))
        except Exception as e:  # network / timeout / parse
            last_err = e
            logger.warning("OpenRouter call failed (attempt %d): %s", attempt + 1, e)
            await asyncio.sleep(min(20.0, 3.0 * (2 ** attempt)))
    raise RuntimeError(f"OpenRouter call failed after {max_retries} attempts: {last_err}")


async def chat(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    schema: Optional[Dict[str, Any]] = None,
    temperature: float = 0.7,
    num_ctx: int = 8192,
    num_predict: int = 6144,
    timeout: float = 420.0,
    keep_alive: str = "30m",
    max_retries: int = 4,
) -> str:
    """One chat call, with a HARD overall deadline across all retries.

    `timeout` bounds a single attempt; with `max_retries` + exponential backoff the retry loop
    could otherwise run for the better part of an hour on a flaky provider and wedge the
    pipeline step behind it. `_deadline()` caps the whole thing so a hang always surfaces as a
    failed step, never as a run stuck in `running` forever.
    """
    return await asyncio.wait_for(
        _chat_impl(messages, model=model, schema=schema, temperature=temperature,
                   num_ctx=num_ctx, num_predict=num_predict, timeout=timeout,
                   keep_alive=keep_alive, max_retries=max_retries),
        timeout=_deadline(timeout),
    )


def _deadline(timeout: float) -> float:
    """Overall wall-clock budget for one `chat()` (all attempts + backoff included)."""
    env = os.environ.get("BUILDERAPPS_LLM_DEADLINE_SEC", "").strip()
    if env:
        try:
            return max(30.0, float(env))
        except ValueError:
            pass
    return min(3.0 * float(timeout), 900.0)


async def _chat_impl(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    schema: Optional[Dict[str, Any]] = None,
    temperature: float = 0.7,
    num_ctx: int = 8192,
    num_predict: int = 6144,
    timeout: float = 420.0,
    keep_alive: str = "30m",
    max_retries: int = 4,
) -> str:
    """`schema` requests JSON output (plan/classify steps). Returns the assistant message
    content. Routes to OpenRouter/Kimi when configured, else Ollama."""
    if OPENROUTER_API_KEY:
        return await _openrouter_chat(messages, schema, temperature, num_predict,
                                      timeout, max_retries)

    base, headers = _endpoint()
    body: Dict[str, Any] = {
        "model": model or TEXT_MODEL,
        "messages": messages,
        "stream": False,
        "think": False,  # qwen3 returns empty without this
        "keep_alive": keep_alive,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }
    if schema is not None:
        body["format"] = schema

    last_err: Optional[Exception] = None
    async with _gpu_sem:
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
                    resp = await client.post(f"{base}/api/chat", json=body, headers=headers)
                if resp.status_code == 503:
                    wait = min(60.0, 5.0 * (2 ** attempt))
                    logger.warning("GPU 503 (loading/queue full), retry in %.0fs", wait)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                content = (data.get("message") or {}).get("content", "")
                if not content.strip():
                    last_err = RuntimeError("GPU returned empty content")
                    await asyncio.sleep(min(30.0, 3.0 * (2 ** attempt)))
                    continue
                return content
            except httpx.HTTPStatusError as e:
                last_err = e
                logger.warning("GPU HTTP %s (attempt %d)", e.response.status_code, attempt + 1)
                await asyncio.sleep(min(30.0, 3.0 * (2 ** attempt)))
            except Exception as e:  # network / timeout
                last_err = e
                logger.warning("GPU call failed (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(min(30.0, 3.0 * (2 ** attempt)))

    raise RuntimeError(f"GPU call failed after {max_retries} attempts: {last_err}")


# --- tool calling (phase 25) -----------------------------------------------
# The agentic codegen loop needs the model to *use* tools, not describe them. Both backends
# speak an OpenAI-ish tool protocol: OpenRouter/Kimi returns `message.tool_calls` with
# `function.arguments` as a JSON STRING; Ollama returns the same shape but with arguments
# already decoded to an object and no call ids. `chat_tools()` normalizes both to
#     {"content": str, "tool_calls": [{"id","name","arguments"}], "finish_reason": str,
#      "raw": <provider message dict>, "usage": {...}}
# and leaves the loop itself to the caller (server.harness.agentic), which is where the
# transcript is persisted and the call budget is enforced.
#
# `chat()` above is untouched — strategy docs, schema design and the QA critic still use it.


def _normalize_tool_calls(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = (tc or {}).get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        out.append({
            "id": tc.get("id") or f"call_{i}",
            "name": name,
            # may be a JSON string (OpenRouter) or an object (Ollama) — the toolbox accepts
            # either and repairs malformed JSON rather than crashing (provider quirk).
            "arguments": fn.get("arguments", {}),
        })
    return out


async def _openrouter_tools(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]],
                            temperature: float, num_predict: int, timeout: float,
                            max_retries: int, tool_choice: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max(int(num_predict), 4000),
        "tools": tools,
        "reasoning": {"enabled": False},
        "provider": {"sort": "throughput"},
        # Same as the plain chat path: ask for the REAL cost + cached-token counts. The
        # agentic codegen loop is where most of a build's tokens go, so without this the
        # Usage tab priced the expensive half of every build from the estimate table.
        "usage": {"include": True},
    }
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://builderapps.osmike.com",
        "X-Title": "MikeOS builderapps",
    }
    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(OPENROUTER_URL, json=body, headers=headers)
            if resp.status_code in (408, 409, 429, 500, 502, 503, 520, 524, 529):
                wait = min(40.0, 4.0 * (2 ** attempt))
                logger.warning("OpenRouter(tools) %s, retry in %.0fs", resp.status_code, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            choice = (data.get("choices") or [{}])[0] or {}
            msg = choice.get("message") or {}
            calls = _normalize_tool_calls(msg)
            content = msg.get("content") or ""
            # Empty content is NORMAL while the model is calling a tool — only a reply with
            # neither content nor a tool call is a failure worth retrying.
            if not content.strip() and not calls:
                last_err = RuntimeError("OpenRouter returned neither content nor a tool call")
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            return {"content": content, "tool_calls": calls,
                    "finish_reason": choice.get("finish_reason") or "", "raw": msg,
                    "usage": _report_usage(data.get("usage") or {},
                                           data.get("model") or LLM_MODEL)}
        except httpx.HTTPStatusError as e:
            last_err = e
            logger.warning("OpenRouter(tools) HTTP %s (attempt %d)",
                           e.response.status_code, attempt + 1)
            await asyncio.sleep(min(20.0, 3.0 * (2 ** attempt)))
        except Exception as e:  # network / timeout / parse
            last_err = e
            logger.warning("OpenRouter(tools) failed (attempt %d): %s", attempt + 1, e)
            await asyncio.sleep(min(20.0, 3.0 * (2 ** attempt)))
    raise RuntimeError(f"OpenRouter tool call failed after {max_retries} attempts: {last_err}")


async def _ollama_tools(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]],
                        temperature: float, num_ctx: int, num_predict: int,
                        timeout: float, max_retries: int) -> Dict[str, Any]:
    base, headers = _endpoint()
    body: Dict[str, Any] = {
        "model": TEXT_MODEL,
        "messages": messages,
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "tools": tools,
        "options": {"temperature": temperature, "num_ctx": num_ctx,
                    "num_predict": num_predict},
    }
    last_err: Optional[Exception] = None
    async with _gpu_sem:
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
                    resp = await client.post(f"{base}/api/chat", json=body, headers=headers)
                if resp.status_code == 503:
                    await asyncio.sleep(min(60.0, 5.0 * (2 ** attempt)))
                    continue
                resp.raise_for_status()
                data = resp.json()
                msg = data.get("message") or {}
                calls = _normalize_tool_calls(msg)
                content = msg.get("content") or ""
                if not content.strip() and not calls:
                    last_err = RuntimeError("GPU returned neither content nor a tool call")
                    await asyncio.sleep(min(30.0, 3.0 * (2 ** attempt)))
                    continue
                return {"content": content, "tool_calls": calls,
                        "finish_reason": data.get("done_reason") or "", "raw": msg,
                        "usage": {}}
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("GPU tool call failed (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(min(30.0, 3.0 * (2 ** attempt)))
    raise RuntimeError(f"GPU tool call failed after {max_retries} attempts: {last_err}")


async def chat_tools(
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    *,
    temperature: float = 0.2,
    num_ctx: int = 32768,
    num_predict: int = 8000,
    timeout: float = 300.0,
    max_retries: int = 4,
    tool_choice: Any = None,
) -> Dict[str, Any]:
    """ONE tool-enabled turn. Returns the normalized assistant message (see above).

    Same hard overall-deadline discipline as `chat()`: a provider hang must fail the step,
    never wedge the run. The caller feeds each result back as a `role:"tool"` message and
    calls this again — the loop, its budget and its transcript live in the harness.
    """
    impl = (_openrouter_tools(messages, tools, temperature, num_predict, timeout,
                              max_retries, tool_choice)
            if OPENROUTER_API_KEY else
            _ollama_tools(messages, tools, temperature, num_ctx, num_predict,
                          timeout, max_retries))
    return await asyncio.wait_for(impl, timeout=_deadline(timeout))


def tool_result_message(call_id: str, name: str, content: str) -> Dict[str, Any]:
    """Build the `role:"tool"` message that feeds one tool's output back to the model."""
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


def assistant_tool_message(reply: Dict[str, Any]) -> Dict[str, Any]:
    """Re-serialize a `chat_tools` reply as the assistant turn to append to the history.

    The provider's own message dict is used verbatim when available so tool_call ids match
    exactly what the API expects back (a mismatch is a 400 from OpenAI-compatible endpoints).
    """
    raw = reply.get("raw")
    if isinstance(raw, dict) and raw.get("role"):
        return raw
    msg: Dict[str, Any] = {"role": "assistant", "content": reply.get("content") or ""}
    if reply.get("tool_calls"):
        msg["tool_calls"] = [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"],
                          "arguments": c["arguments"] if isinstance(c["arguments"], str)
                          else json.dumps(c["arguments"])}}
            for c in reply["tool_calls"]
        ]
    return msg


# --- streaming -------------------------------------------------------------
async def _openrouter_stream(messages: List[Dict[str, Any]], temperature: float,
                             num_predict: int, timeout: float) -> AsyncIterator[str]:
    """Stream content deltas from OpenRouter (Kimi). Yields text chunks as they arrive."""
    or_max = 2000 if num_predict <= 1024 else max(int(num_predict), 12000)
    body: Dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": or_max,
        "stream": True,
        "reasoning": {"enabled": False},
        "provider": {"sort": "throughput"},
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://builderapps.osmike.com",
        "X-Title": "MikeOS builderapps",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", OPENROUTER_URL, json=body, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                except Exception:
                    continue
                if data.get("error"):
                    raise RuntimeError(str(data["error"]))
                delta = (((data.get("choices") or [{}])[0] or {}).get("delta") or {})
                chunk = delta.get("content")
                if chunk:
                    yield chunk


async def _ollama_stream(messages: List[Dict[str, Any]], temperature: float,
                         num_ctx: int, num_predict: int, timeout: float,
                         keep_alive: str) -> AsyncIterator[str]:
    """Stream content deltas from Ollama (newline-delimited JSON). Yields text chunks."""
    base, headers = _endpoint()
    body: Dict[str, Any] = {
        "model": TEXT_MODEL,
        "messages": messages,
        "stream": True,
        "think": False,
        "keep_alive": keep_alive,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }
    async with _gpu_sem:
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            async with client.stream("POST", f"{base}/api/chat", json=body,
                                     headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue
                    if data.get("error"):
                        raise RuntimeError(str(data["error"]))
                    chunk = (data.get("message") or {}).get("content")
                    if chunk:
                        yield chunk
                    if data.get("done"):
                        break


async def stream_chat(
    messages: List[Dict[str, Any]],
    *,
    temperature: float = 0.4,
    num_ctx: int = 8192,
    num_predict: int = 8192,
    timeout: float = 420.0,
    keep_alive: str = "30m",
) -> AsyncIterator[str]:
    """Stream a chat completion, yielding content deltas as they arrive. Routes to
    OpenRouter/Kimi when configured, else Ollama. On a stream error, callers should treat
    the accumulated text as the (possibly partial) result — the harness re-sanitizes it."""
    if OPENROUTER_API_KEY:
        async for chunk in _openrouter_stream(messages, temperature, num_predict, timeout):
            yield chunk
    else:
        async for chunk in _ollama_stream(messages, temperature, num_ctx, num_predict,
                                          timeout, keep_alive):
            yield chunk
