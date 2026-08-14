"""OpenAI-compatible LLM proxy for assistant beat containers (phase 30).

## Why this exists

The assistant's coding engine is **Pi** (`@earendil-works/pi-coding-agent`), and Pi runs
*inside the beat container* — that is where the checkout and the CLI tools are. But Pi needs
a model endpoint, and the one thing this platform will not do is put a real provider key
inside an LLM-driven container. A key in there is a key that an agent can read, print into a
commit, or exfiltrate through the app it is writing.

So the container never sees one. It is pointed at this proxy and authenticates with the
credential it already holds — its per-assistant `asst_…` token — and the proxy:

  * swaps that token for the real `OPENROUTER_API_KEY`, server-side;
  * **pins the model.** Whatever `model` the client asks for is discarded and replaced with
    `BUILDERAPPS_LLM_MODEL` (Kimi k3). Otherwise a container could name any model on
    OpenRouter and spend accordingly;
  * keeps every call inside the platform's **usage accounting** — the same pricing path
    `gpu.chat()` uses (`gpu.account_usage`), attributed to `assistant:<id>` so an assistant
    shows up in the project's Usage tab like everything else;
  * enforces a **per-beat spend cap**, which is the only real backstop available: Pi has no
    `--max-turns` or cost limit of its own (verified against 0.84.2 — there is no such flag),
    so an agent that loops would otherwise loop on our money. When a beat is over budget the
    proxy stops answering with model output and returns a plain completion telling the agent
    to stop, which ends Pi's loop cleanly instead of crashing it mid-edit.

## Streaming is not optional

Pi's `openai-completions` client always sends `stream: true` with
`stream_options.include_usage` (`pi-ai/dist/api/openai-completions.js` `buildParams`), so
this proxy streams the SSE body straight through and sniffs the final `usage` chunk on the
way past for accounting. It never buffers a whole response.
"""
import json
import logging
import os
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from server import assistants as A
from server import gpu, usage

logger = logging.getLogger(__name__)

router = APIRouter(tags=["assistant-llm"])

OPENROUTER_URL = os.environ.get("OPENROUTER_CHAT_URL",
                               "https://openrouter.ai/api/v1/chat/completions")
# The ONE model an assistant container may spend on, whatever it asks for.
PINNED_MODEL = os.environ.get("BUILDERAPPS_LLM_MODEL", "moonshotai/kimi-k3").strip()
# Kimi k3 is a reasoning model; the rest of this platform runs it with reasoning OFF (it
# roughly doubles latency and the loop is already tool-driven). Overridable per deployment.
REASONING = os.environ.get("ASSISTANT_LLM_REASONING", "0").strip() in ("1", "true", "yes")
# The spend backstop, in USD, for ONE beat. Pi has no built-in step/turn/cost cap, so this
# is it. Generous enough for a real feature, small enough that a runaway is a rounding error.
BEAT_COST_CAP = float(os.environ.get("ASSISTANT_BEAT_COST_CAP_USD", "1.50"))
REQUEST_TIMEOUT = float(os.environ.get("ASSISTANT_LLM_TIMEOUT_SEC", "600"))

# beat_id -> USD spent through this proxy. In-memory on purpose: the control plane is a
# single process (one uvicorn worker, see the Dockerfile) and a beat lives for minutes, so
# a restart losing the counter is harmless — the beat it belonged to died with it.
_beat_cost: dict[int, float] = {}
_MAX_TRACKED = 500


def beat_cost(beat_id: Optional[int]) -> float:
    """What this beat has spent through the proxy so far (i.e. what Pi cost)."""
    if beat_id is None:
        return 0.0
    return round(_beat_cost.get(int(beat_id), 0.0), 6)


def forget_beat(beat_id: Optional[int]) -> float:
    """Read the beat's proxy spend and drop the counter. Called once the beat is recorded."""
    if beat_id is None:
        return 0.0
    return round(_beat_cost.pop(int(beat_id), 0.0), 6)


def _charge(beat_id: Optional[int], amount: float) -> None:
    if beat_id is None or not amount:
        return
    bid = int(beat_id)
    _beat_cost[bid] = _beat_cost.get(bid, 0.0) + float(amount)
    if len(_beat_cost) > _MAX_TRACKED:                      # bounded memory
        for old in list(_beat_cost)[: len(_beat_cost) - _MAX_TRACKED]:
            _beat_cost.pop(old, None)


async def _assistant_from_auth(authorization: str) -> dict:
    """The `Authorization: Bearer <asst_…>` Pi sends is the assistant's own credential."""
    tok = (authorization or "").strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    a = await A.get_by_token(tok)
    if not a:
        raise HTTPException(status_code=401, detail="invalid assistant token")
    return a


def _sse(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _stop_completion(model: str, text: str) -> dict:
    return {"id": "budget-stop", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def _stop_stream(model: str, text: str):
    """A minimal, well-formed SSE completion so Pi ends its turn cleanly.

    Deliberately NOT an HTTP error: an error mid-loop leaves Pi's exit code as the only
    signal and the half-finished edit unexplained. A normal assistant message that says
    "you are out of budget, stop" ends the turn AND lands in the transcript as the reason.
    """
    def gen():
        yield _sse({"id": "budget-stop", "object": "chat.completion.chunk", "model": model,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}}]})
        yield _sse({"id": "budget-stop", "object": "chat.completion.chunk", "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
        yield b"data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/api/assistant/llm/v1/models", summary="[beat container] the one model on offer")
async def list_models(authorization: str = Header("")) -> dict:
    await _assistant_from_auth(authorization)
    return {"object": "list",
            "data": [{"id": "kimi-k3", "object": "model", "owned_by": "builderapps"}]}


@router.post("/api/assistant/llm/v1/chat/completions",
             summary="[beat container] OpenAI-compatible chat completions (key stays here)")
async def chat_completions(request: Request, authorization: str = Header(""),
                           x_beat_id: str = Header("")):
    a = await _assistant_from_auth(authorization)
    aid = int(a["id"])
    project_id = str(a["project_id"])

    beat_id: Optional[int] = None
    raw = (x_beat_id or "").strip()
    if raw.isdigit() and await A.beat_belongs_to(int(raw), aid):
        # A beat id is not a capability — a token scoped to one assistant may only spend
        # against its OWN beat, so a wrong/forged id simply does not attribute (and does not
        # get to reset somebody else's budget counter either).
        beat_id = int(raw)

    try:
        body: Any = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="body must be JSON")
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise HTTPException(status_code=400, detail="expected {model, messages: [...]}")

    spent = beat_cost(beat_id)
    if beat_id is not None and spent >= BEAT_COST_CAP:
        logger.warning("assistant %s beat %s hit the $%.2f cap (spent $%.4f) — stopping it",
                       aid, beat_id, BEAT_COST_CAP, spent)
        text = (f"STOP. This beat has spent ${spent:.4f}, which is over its ${BEAT_COST_CAP:.2f} "
                f"budget. Do not call any more tools. Summarise what you changed so far and "
                f"finish your turn now.")
        return (_stop_stream(PINNED_MODEL, text) if body.get("stream")
                else _stop_completion(PINNED_MODEL, text))

    if not gpu.OPENROUTER_API_KEY:
        raise HTTPException(status_code=503,
                            detail="no model credential is configured on the control plane")

    # Rebuild the upstream body from a KNOWN set of keys. A passthrough would let a container
    # set provider routing, `store`, or a model of its choosing; this way what we forward is
    # what we understand.
    upstream: dict[str, Any] = {
        "model": PINNED_MODEL,                     # PINNED — never the client's choice
        "messages": body["messages"],
        "stream": bool(body.get("stream")),
        "usage": {"include": True},                # real cost back from OpenRouter
        "reasoning": {"enabled": REASONING},
        "provider": {"sort": "throughput"},
    }
    for k in ("tools", "tool_choice", "temperature", "top_p", "stop", "response_format",
              "parallel_tool_calls", "max_tokens", "stream_options", "seed"):
        if body.get(k) is not None:
            upstream[k] = body[k]
    if upstream["stream"]:
        # Without this the final usage chunk never arrives and the call costs us nothing we
        # can see — the silent-drop failure mode, applied to money.
        upstream["stream_options"] = {"include_usage": True}

    headers = {"Authorization": f"Bearer {gpu.OPENROUTER_API_KEY}",
               "Content-Type": "application/json",
               "HTTP-Referer": "https://builderapps.osmike.com",
               "X-Title": f"builderapps assistant {aid}"}

    def account(u: dict) -> None:
        """Price it exactly like every other call in the platform, attribute it to this
        assistant, and charge it to the beat's budget."""
        if not isinstance(u, dict):
            return
        usage.set_context(project_id, None, f"assistant:{aid}")
        try:
            rec = (gpu.account_usage(u, PINNED_MODEL) or {}).get("_accounted") or {}
            _charge(beat_id, float(rec.get("cost_usd") or 0.0))
        except Exception:  # noqa: BLE001 — accounting must never break the agent
            logger.debug("assistant usage accounting failed", exc_info=True)
        finally:
            usage.clear_context()

    if not upstream["stream"]:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
            r = await c.post(OPENROUTER_URL, json=upstream, headers=headers)
            if r.status_code >= 400:
                logger.warning("openrouter %s for assistant %s: %s", r.status_code, aid,
                               r.text[:300])
                raise HTTPException(status_code=r.status_code, detail=r.text[:600])
            data = r.json()
        account(data.get("usage") or {})
        return data

    async def relay():
        client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        try:
            async with client.stream("POST", OPENROUTER_URL, json=upstream,
                                     headers=headers) as r:
                if r.status_code >= 400:
                    detail = (await r.aread()).decode("utf-8", "replace")[:400]
                    logger.warning("openrouter stream %s for assistant %s: %s",
                                   r.status_code, aid, detail)
                    yield _sse({"error": {"message": detail, "code": r.status_code}})
                    yield b"data: [DONE]\n\n"
                    return
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    yield line.encode() + b"\n\n"
                    if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                        try:
                            chunk = json.loads(line[6:])
                        except Exception:  # noqa: BLE001 — a comment/keepalive line
                            continue
                        if isinstance(chunk, dict) and chunk.get("usage"):
                            account(chunk["usage"])
        finally:
            await client.aclose()

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
