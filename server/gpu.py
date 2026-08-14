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
    """One chat call. `schema` requests JSON output (plan/classify steps). Returns the
    assistant message content. Routes to OpenRouter/Kimi when configured, else Ollama."""
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
