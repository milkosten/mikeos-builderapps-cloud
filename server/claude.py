"""Claude (Anthropic), for the judgement calls that earn it — control plane only.

## Where the credential lives, and where it does not

The token sits on the HOST at `/opt/builderapps/.anthropic-key` (0600, root), mounted
read-only into the control plane and read **once at import**. It is never put in an image,
never passed in a `docker run --env`, never logged, and never reaches a container — the same
rule as `OPENROUTER_API_KEY` (`server/llm_proxy.py`) and chrome-pool's Basic auth
(`server/browser_proxy.py`), and it matters more here: the scout container that this module
exists to serve is the one holding a stranger's source tree.

    docker run --rm --entrypoint sh mikeos-assistant-runtime -c env   # must show nothing

## It is a SUBSCRIPTION OAuth token, not an API key — this changes the design

Verified empirically against the live API rather than assumed:

  * `Authorization: Bearer sk-ant-oat01-…` + `anthropic-beta: oauth-2025-04-20` authenticates.
  * `x-api-key: sk-ant-oat01-…` returns **401 "API key is invalid"** — the header shape is
    not interchangeable, and swapping it is not a key swap.
  * The quota is Mike's personal Claude subscription, SHARED with his own Claude Code
    sessions. On the first live test `claude-opus-4-8` and `claude-sonnet-5` both returned
    **429** while `claude-haiku-4-5` returned 200 — i.e. the good models are frequently
    unavailable, through no fault of ours.

So Claude is an **opportunistic upgrade, never a dependency**: `judge()` walks a model
ladder, and if every rung 429s it returns None and the caller uses Kimi exactly as before.
A feature that broke whenever Mike was busy in Claude Code would be a worse feature than one
that quietly degrades.

## Where it is used

`server/priorart.py`'s `propose()` — reading a repository's evidence and deciding how to put
the offer to a person. That is a judgement call on prose, made once per scout, where the
better model is worth the cost. Ordinary conversation turns stay on Kimi at ~$0.02: they are
the frequent ones, they are latency-sensitive, and they do not need it.

Every call is accounted through the SAME costing path as everything else
(`gpu.account_usage`), labelled with the model that actually served it — so the Usage tab
stays honest about which model produced which turn.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from server import gpu

logger = logging.getLogger(__name__)

API_URL = os.environ.get("ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages")
KEY_FILE = os.environ.get("ANTHROPIC_KEY_FILE", "/opt/builderapps/.anthropic-key")
TIMEOUT = float(os.environ.get("ANTHROPIC_TIMEOUT_SEC", "180"))

# THE LADDER, best first. A 429 on one rung is not an error — it is the subscription being
# busy — so we step down rather than fail. `claude-haiku-4-5` is last because it is the rung
# that answered when the other two were rate-limited, and a Haiku answer beats no answer.
MODELS = [m.strip() for m in os.environ.get(
    "ANTHROPIC_MODELS", "claude-opus-4-8,claude-sonnet-5,claude-haiku-4-5").split(",")
    if m.strip()]

# Published per-MTok pricing, for the Usage tab. Estimated (`cost_estimated=True`) because
# a subscription token is not billed per token at all — the number is what this work WOULD
# have cost on the API, which is the honest thing to show next to the Kimi rows.
PRICES: Dict[str, tuple] = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

_TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")


def _load_token() -> str:
    tok = (os.environ.get("ANTHROPIC_OAUTH_TOKEN") or "").strip()
    if tok:
        return tok
    try:
        with open(KEY_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


_TOKEN = _load_token()


def available() -> bool:
    return bool(_TOKEN)


def redact(text: str) -> str:
    """Belt and braces. Nothing here should ever put the token in a string, but the one
    place a credential leaks is always the error path nobody wrote a test for."""
    return _TOKEN_RE.sub("sk-ant-***", text or "")


def _headers() -> Dict[str, str]:
    # OAuth tokens go on `Authorization: Bearer`, NOT `x-api-key` — verified: the latter
    # returns 401. The `oauth-2025-04-20` beta is required for /v1/messages with this
    # credential; without it the request is rejected.
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "content-type": "application/json",
    }


def _account(model: str, usage: Dict[str, Any]) -> float:
    """Price it and push it down the same sink `gpu.chat` uses, so an Anthropic turn shows
    up in the project's Usage tab beside the Kimi ones instead of being invisible spend."""
    pin, pout = PRICES.get(model, (5.00, 25.00))
    pt = int(usage.get("input_tokens") or 0)
    ct = int(usage.get("output_tokens") or 0)
    cached = int(usage.get("cache_read_input_tokens") or 0)
    cost = (pt * pin + ct * pout) / 1_000_000.0
    rec = {"model": model, "prompt_tokens": pt, "completion_tokens": ct,
           "cached_tokens": cached, "cost_usd": round(cost, 6), "cost_estimated": True}
    try:
        sink = gpu.get_usage_sink()
        if sink:
            sink(rec)
    except Exception:  # noqa: BLE001 — accounting must never break a feature
        logger.debug("anthropic usage sink failed", exc_info=True)
    return rec["cost_usd"]


async def judge(system: str, user: str, *, max_tokens: int = 1500,
                models: Optional[List[str]] = None) -> Optional[dict]:
    """One Claude call. Returns {text, model, cost_usd} — or **None**, which means
    "unavailable, use Kimi".

    None is a first-class, expected answer, not an error: the subscription is shared with a
    human's own Claude usage, so 429 is routine. Every caller must have a Kimi path.
    """
    if not _TOKEN:
        return None
    ladder = models or MODELS
    last = ""
    for model in ladder:
        body = {"model": model, "max_tokens": max_tokens,
                "system": system, "messages": [{"role": "user", "content": user}]}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.post(API_URL, headers=_headers(), json=body)
        except Exception as e:  # noqa: BLE001
            last = f"{model}: {redact(str(e))[:160]}"
            logger.info("anthropic %s unreachable: %s", model, last)
            continue
        if r.status_code == 429:
            # The subscription is busy — almost always because a human is using it. Step
            # down the ladder; do not retry the same rung, that just waits for nothing.
            logger.info("anthropic %s rate-limited (subscription shared) — next rung", model)
            last = f"{model}: 429"
            continue
        if r.status_code >= 400:
            last = f"{model}: HTTP {r.status_code} {redact(r.text)[:200]}"
            logger.warning("anthropic %s refused: %s", model, last)
            continue
        try:
            data = r.json()
            text = "".join(b.get("text") or "" for b in (data.get("content") or [])
                           if b.get("type") == "text")
        except Exception as e:  # noqa: BLE001
            last = f"{model}: unparseable ({e})"
            continue
        if not text.strip():
            # A refusal or an empty completion is not something to dress up as an answer.
            last = f"{model}: empty completion (stop_reason={data.get('stop_reason')})"
            logger.info("anthropic %s returned nothing: %s", model, last)
            continue
        cost = _account(model, data.get("usage") or {})
        return {"text": text, "model": model, "cost_usd": cost}
    logger.info("anthropic unavailable, falling back to the default model (%s)", last)
    return None


def extract_json(text: str) -> Any:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        return json.loads(t[i:j + 1])
    raise ValueError("no parseable JSON")
