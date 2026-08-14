"""A real browser for the assistants — proxied, so the credential never enters a container.

## Why this exists

A Developer assistant was asked why the site was broken. It read the deployment record —
health green, every ship step clean — and concluded that "an SSL error is a platform-ingress
issue (certificate provisioning on the domain)". It was confidently, expensively wrong: the
real cause was a `Content-Security-Policy: frame-ancestors 'none'` header it had added
itself, which blocked the builder's preview iframe. Nothing it could reach could have told
it that. `curl` and `/health` prove a process is listening; they cannot prove a UI works.

So every assistant now gets a browser. Not instead of curl — as well as it.

## Why it is a PROXY and not a client in the container

chrome-pool authenticates with ONE shared Basic credential used by the whole estate. Putting
it in an LLM-driven container means an agent can read it, print it into a commit message, or
bake it into the app it is writing. It is exactly the reasoning behind `server/llm_proxy.py`
for the model key, and the answer is the same: the container authenticates to the CONTROL
PLANE with the per-assistant `asst_…` token it already holds, and the control plane talks to
chrome-pool with the real credential. That also buys three things a direct client cannot:

  * **an allow-list** — an assistant browses its OWN project and nothing else, so a compromised
    or merely over-curious agent cannot use our shared browser fleet (and our IP) to read the
    internet;
  * **an audit trail** — every navigation is logged with the assistant and beat that caused it;
  * **a session ledger with a guaranteed close** — see below, because this is where the
    existing code was already leaking.

## Leaked sessions were REAL, not theoretical

chrome-pool closes a session with `DELETE /session/{id}`. There is no `POST
/session/{id}/close` — that route 404s. Every close in this codebase (and in the clients
copied from it) was firing at a non-existent endpoint inside a `try/except: pass`, so every
session survived until chrome-pool's own 30-minute TTL. The pool was sitting at 11 live
sessions with nobody using it. `close_session()` below is the one correct implementation and
`server/chrome.py` now calls it too.

Closing is belt AND braces: the CLI closes when it is done, the beat closes everything it
opened before it exits, and `close_beat_sessions()` runs again when the beat record lands —
so a container that is OOM-killed mid-navigation still cannot leak.
"""
import asyncio
import logging
import os
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from server import assistants as A
from server import chrome

logger = logging.getLogger(__name__)

router = APIRouter(tags=["assistant-browser"])

SITES_BASE = os.environ.get("SITES_BASE", "builderapps.osmike.com")
PUBLIC_API_BASE = os.environ.get("PUBLIC_API_BASE",
                                 "https://builderapps-api.osmike.com").rstrip("/")

# --- the bounds -----------------------------------------------------------
# A browser is a shared resource with a fixed number of Chromes. An assistant that opens one
# per thought would starve the estate, so a beat gets a small, fixed allowance.
MAX_SESSIONS_PER_BEAT = int(os.environ.get("ASSISTANT_BROWSER_MAX_SESSIONS", "3"))
MAX_NAVS_PER_BEAT = int(os.environ.get("ASSISTANT_BROWSER_MAX_NAVS", "30"))
NAV_TIMEOUT_SEC = float(os.environ.get("ASSISTANT_BROWSER_NAV_TIMEOUT", "45"))
CALL_TIMEOUT_SEC = float(os.environ.get("ASSISTANT_BROWSER_CALL_TIMEOUT", "30"))
# A session older than this is reaped even if nobody asked. chrome-pool's own TTL is 30 min;
# a beat that needs a browser open longer than 10 has stopped making progress.
SESSION_TTL_SEC = float(os.environ.get("ASSISTANT_BROWSER_SESSION_TTL", "600"))

# The self-test fixture below is served by THIS app on its public host, so chrome-pool (which
# lives on another box) can load it. It is the one non-project URL an assistant may open.
SELFTEST_PATH = "/api/assistant/browser/selftest"
SELFTEST_URL = os.environ.get("ASSISTANT_BROWSER_SELFTEST_URL",
                              PUBLIC_API_BASE + SELFTEST_PATH)

# sid -> {assistant_id, beat_id, opened, last, navs, url}
# In-memory on purpose, exactly like the LLM proxy's per-beat budget: the control plane is a
# single process and a session lives for minutes. A restart loses the ledger, but it also
# kills every beat that owned one, and chrome-pool's TTL collects the remains.
_sessions: dict[str, dict] = {}
_beat_navs: dict[int, int] = {}
_MAX_TRACKED_BEATS = 500


# ---------------------------------------------------------------------------
# who is calling
# ---------------------------------------------------------------------------
async def _caller(token: str, beat_hdr: str) -> tuple[dict, Optional[int]]:
    """The `asst_…` token identifies the assistant; the beat id is verified, never trusted.

    A beat id is not a capability — an assistant may only spend its OWN beat's allowance, so
    a wrong or forged id simply does not bind (and cannot reset somebody else's counters).
    """
    tok = (token or "").strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    a = await A.get_by_token(tok)
    if not a:
        raise HTTPException(status_code=401, detail="invalid assistant token")
    beat_id: Optional[int] = None
    raw = (beat_hdr or "").strip()
    if raw.isdigit() and await A.beat_belongs_to(int(raw), int(a["id"])):
        beat_id = int(raw)
    return a, beat_id


def _require_browser(assistant: dict) -> None:
    """`run_qa` is the browser capability. An assistant without it is not offered a browser
    and is refused one if it asks anyway — the same choke point as every other act."""
    try:
        A.require(assistant, "run_qa")
    except A.Denied as e:
        raise HTTPException(status_code=403, detail=str(e))


# ---------------------------------------------------------------------------
# where it may go
# ---------------------------------------------------------------------------
def allowed_url(assistant: dict, url: str) -> tuple[bool, str]:
    """Default-deny. An assistant browses its OWN project's site, and the self-test fixture.

    Note what is deliberately NOT allowed: **loopback**. The browser is not in the
    assistant's container — it is a shared Chrome on another box, so `localhost` there means
    *chrome-pool's own container*, never the app under test. Allowing it would be a pointless
    SSRF hole into a shared service in exchange for nothing. (chrome-pool blocks it at its
    own edge too; this is the belt.) If an assistant ever needs a local URL, the answer is to
    give it a real hostname, not to open loopback.
    """
    u = (url or "").strip()
    if not u:
        return False, "no url"
    if len(u) > 2048:
        return False, "url is absurdly long"
    try:
        p = urlparse(u)
    except Exception:  # noqa: BLE001
        return False, "unparseable url"
    if p.scheme not in ("http", "https"):
        return False, f"scheme {p.scheme or '(none)'} is not allowed — http/https only"
    host = (p.hostname or "").lower()
    if not host:
        return False, "no host in url"
    project_id = str(assistant.get("project_id") or "").strip().lower()
    if project_id and host == f"{project_id}.{SITES_BASE}".lower():
        return True, ""
    # The fixture, exact host AND exact path: it is a regression test, not a doorway.
    sp = urlparse(SELFTEST_URL)
    if host == (sp.hostname or "").lower() and p.path.rstrip("/") == sp.path.rstrip("/"):
        return True, ""
    return False, (f"refused: {host} is not this assistant's app. You may browse "
                   f"https://{project_id}.{SITES_BASE}/ (your own project) only.")


# ---------------------------------------------------------------------------
# chrome-pool, spoken with the real credential
# ---------------------------------------------------------------------------
def _auth() -> tuple[str, str]:
    return (chrome.CHROME_POOL_USER, chrome.CHROME_POOL_PASS)


async def _pool(method: str, path: str, *, json_body: Optional[dict] = None,
                timeout: float = CALL_TIMEOUT_SEC) -> Any:
    """One call to chrome-pool. Raises HTTPException with a message an LLM can act on."""
    url = chrome.CHROME_POOL_URL + path
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False, auth=_auth()) as c:
            r = await c.request(method, url, json=json_body)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504,
                            detail=f"the browser did not respond within {timeout:.0f}s")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"the browser is unreachable: {e}"[:300])
    if r.status_code >= 400:
        body = (r.text or "")[:300]
        raise HTTPException(status_code=502,
                            detail=f"browser returned {r.status_code}: {body}")
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {}


async def close_session(sid: str) -> bool:
    """Close ONE chrome-pool session. `DELETE /session/{id}` — see the module docstring for
    why this is worth its own function and a comment."""
    if not sid:
        return False
    _sessions.pop(sid, None)
    try:
        async with httpx.AsyncClient(timeout=15, verify=False, auth=_auth()) as c:
            r = await c.delete(f"{chrome.CHROME_POOL_URL}/session/{sid}")
        # 404 means it is already gone, which is the state we wanted.
        return r.status_code < 400 or r.status_code == 404
    except Exception as e:  # noqa: BLE001 — a close that fails must not raise into a beat
        logger.info("browser: could not close session %s: %s", sid, e)
        return False


async def close_beat_sessions(beat_id: Optional[int]) -> int:
    """Close everything a beat left open. Called when the beat record lands, so a container
    that died mid-navigation still cannot leak a session into the shared pool."""
    if beat_id is None:
        return 0
    bid = int(beat_id)
    _beat_navs.pop(bid, None)
    stale = [sid for sid, s in _sessions.items() if s.get("beat_id") == bid]
    for sid in stale:
        await close_session(sid)
    if stale:
        logger.info("browser: closed %d leftover session(s) for beat %s", len(stale), bid)
    return len(stale)


async def _reap_expired() -> None:
    now = time.monotonic()
    for sid, s in list(_sessions.items()):
        if now - float(s.get("opened") or now) > SESSION_TTL_SEC:
            logger.info("browser: reaping session %s (older than %ss)", sid, SESSION_TTL_SEC)
            await close_session(sid)


def _own(sid: str, assistant: dict) -> dict:
    """A session id is a capability here, so it is checked against the ledger — an assistant
    may only drive a session IT opened, never one it guessed or read somewhere."""
    s = _sessions.get(sid)
    if not s or s.get("assistant_id") != int(assistant["id"]):
        raise HTTPException(status_code=404,
                            detail="no such browser session (it is closed, expired, or not "
                                   "yours) — open a new one")
    s["last"] = time.monotonic()
    return s


# ---------------------------------------------------------------------------
# the collectors — installed for the agent, so `console` always works
# ---------------------------------------------------------------------------
# Window state does NOT survive a navigate, so these are installed by the proxy immediately
# AFTER every navigation rather than being something the agent has to remember. An agent that
# must set up its own instrumentation before it can see an error will forget, and then report
# a clean page.
_COLLECTORS_JS = chrome._INSTALL_COLLECTORS_JS


class OpenBody(BaseModel):
    url: Optional[str] = None


class NavBody(BaseModel):
    url: str


class EvalBody(BaseModel):
    expression: str


class ClickBody(BaseModel):
    selector: str
    timeout_ms: Optional[int] = None


class TypeBody(BaseModel):
    selector: str
    text: str


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.post("/api/assistant/browser/session",
             summary="[beat container] open a browser session (credential stays here)")
async def open_session(body: OpenBody, x_assistant_token: str = Header(""),
                       x_beat_id: str = Header("")) -> dict:
    a, beat_id = await _caller(x_assistant_token, x_beat_id)
    _require_browser(a)
    await _reap_expired()
    if beat_id is not None:
        mine = [s for s in _sessions.values() if s.get("beat_id") == beat_id]
        if len(mine) >= MAX_SESSIONS_PER_BEAT:
            raise HTTPException(
                status_code=429,
                detail=f"this beat already has {len(mine)} browser session(s) open "
                       f"(the cap is {MAX_SESSIONS_PER_BEAT}) — close one before opening "
                       f"another")
    data = await _pool("POST", "/session")
    sid = str((data or {}).get("sessionId") or (data or {}).get("id") or "")
    if not sid:
        raise HTTPException(status_code=502, detail="the browser gave back no session id")
    _sessions[sid] = {"assistant_id": int(a["id"]), "beat_id": beat_id,
                      "opened": time.monotonic(), "last": time.monotonic(),
                      "navs": 0, "url": ""}
    logger.info("browser: assistant %s beat %s opened session %s", a["id"], beat_id, sid)
    out = {"session": sid, "allowed": f"https://{a.get('project_id')}.{SITES_BASE}/"}
    if body.url:
        out["navigated"] = await _navigate(a, beat_id, sid, body.url)
    return out


async def _navigate(assistant: dict, beat_id: Optional[int], sid: str, url: str) -> dict:
    ok, why = allowed_url(assistant, url)
    if not ok:
        raise HTTPException(status_code=403, detail=why)
    s = _own(sid, assistant)
    if beat_id is not None:
        n = _beat_navs.get(beat_id, 0)
        if n >= MAX_NAVS_PER_BEAT:
            raise HTTPException(
                status_code=429,
                detail=f"this beat has already loaded {n} pages (the cap is "
                       f"{MAX_NAVS_PER_BEAT}) — you are browsing, not verifying. Stop and "
                       f"report what you found.")
        _beat_navs[beat_id] = n + 1
        if len(_beat_navs) > _MAX_TRACKED_BEATS:            # bounded memory
            for old in list(_beat_navs)[: len(_beat_navs) - _MAX_TRACKED_BEATS]:
                _beat_navs.pop(old, None)
    # AUDIT: every page an assistant loads, with who and which beat.
    logger.info("browser: assistant %s beat %s -> %s", assistant["id"], beat_id, url[:200])
    out = await _pool("POST", f"/session/{sid}/navigate",
                      json_body={"url": url, "acceptCookies": True},
                      timeout=NAV_TIMEOUT_SEC)
    s["navs"] = int(s.get("navs") or 0) + 1
    s["url"] = str((out or {}).get("url") or url)
    # Give the page a moment to run its scripts, then instrument it. Both matter: without the
    # pause a fast check reads an empty page, and without the collectors `console` reports a
    # clean run on a page that threw.
    await asyncio.sleep(1.0)
    try:
        await _pool("POST", f"/session/{sid}/eval", json_body={"expression": _COLLECTORS_JS})
    except HTTPException:
        logger.info("browser: could not install collectors on %s", sid)
    return {"url": s["url"]}


@router.post("/api/assistant/browser/session/{sid}/navigate",
             summary="[beat container] load a page (allow-listed to the assistant's own app)")
async def navigate(sid: str, body: NavBody, x_assistant_token: str = Header(""),
                   x_beat_id: str = Header("")) -> dict:
    a, beat_id = await _caller(x_assistant_token, x_beat_id)
    _require_browser(a)
    return await _navigate(a, beat_id, sid, body.url)


@router.get("/api/assistant/browser/session/{sid}/text",
            summary="[beat container] the rendered page as text")
async def page_text(sid: str, x_assistant_token: str = Header(""),
                    x_beat_id: str = Header("")) -> dict:
    a, _ = await _caller(x_assistant_token, x_beat_id)
    _require_browser(a)
    _own(sid, a)
    data = await _pool("GET", f"/session/{sid}/snapshot")
    return {"text": str((data or {}).get("text") or "")}


@router.post("/api/assistant/browser/session/{sid}/eval",
             summary="[beat container] evaluate JS in the page")
async def eval_js(sid: str, body: EvalBody, x_assistant_token: str = Header(""),
                  x_beat_id: str = Header("")) -> dict:
    a, _ = await _caller(x_assistant_token, x_beat_id)
    _require_browser(a)
    _own(sid, a)
    data = await _pool("POST", f"/session/{sid}/eval",
                       json_body={"expression": body.expression[:20000]})
    return {"value": (data or {}).get("value")}


@router.post("/api/assistant/browser/session/{sid}/click",
             summary="[beat container] click a selector")
async def click(sid: str, body: ClickBody, x_assistant_token: str = Header(""),
                x_beat_id: str = Header("")) -> dict:
    a, _ = await _caller(x_assistant_token, x_beat_id)
    _require_browser(a)
    _own(sid, a)
    await _pool("POST", f"/session/{sid}/click",
                json_body={"selector": body.selector[:400],
                           "timeoutMs": int(body.timeout_ms or 5000)})
    await asyncio.sleep(0.6)          # let the handler and any XHR it fired actually run
    return {"ok": True}


@router.post("/api/assistant/browser/session/{sid}/type",
             summary="[beat container] type into a selector")
async def type_text(sid: str, body: TypeBody, x_assistant_token: str = Header(""),
                    x_beat_id: str = Header("")) -> dict:
    a, _ = await _caller(x_assistant_token, x_beat_id)
    _require_browser(a)
    _own(sid, a)
    await _pool("POST", f"/session/{sid}/type",
                json_body={"selector": body.selector[:400], "text": body.text[:4000]})
    return {"ok": True}


@router.get("/api/assistant/browser/session/{sid}/console",
            summary="[beat container] JS errors + failed requests since the last navigation")
async def console(sid: str, x_assistant_token: str = Header(""),
                  x_beat_id: str = Header("")) -> dict:
    """What curl cannot tell you: what the PAGE did once a browser ran it."""
    a, _ = await _caller(x_assistant_token, x_beat_id)
    _require_browser(a)
    _own(sid, a)
    errs = await _pool("POST", f"/session/{sid}/eval",
                       json_body={"expression": "JSON.stringify(window.__errs||[])"})
    net = await _pool("POST", f"/session/{sid}/eval",
                      json_body={"expression": "JSON.stringify(window.__net||[])"})
    instrumented = await _pool("POST", f"/session/{sid}/eval",
                               json_body={"expression": "!!window.__qa"})
    return {
        "console_errors": chrome._parse_json_list((errs or {}).get("value"))[:25],
        "failed_requests": chrome._parse_json_list((net or {}).get("value"))[:25],
        # NEVER TRUST A CLEAN RESULT YOU DID NOT EARN. If the collectors are not installed,
        # empty lists mean "we saw nothing", not "nothing happened" — say which it is.
        "instrumented": bool((instrumented or {}).get("value")),
    }


@router.post("/api/assistant/browser/session/{sid}/exercise",
             summary="[beat container] click through the page like an impatient user")
async def exercise(sid: str, x_assistant_token: str = Header(""),
                   x_beat_id: str = Header("")) -> dict:
    a, _ = await _caller(x_assistant_token, x_beat_id)
    _require_browser(a)
    _own(sid, a)
    data = await _pool("POST", f"/session/{sid}/eval",
                       json_body={"expression": chrome._EXERCISE_JS})
    await asyncio.sleep(1.5)
    return {"clicked": (data or {}).get("value")}


@router.get("/api/assistant/browser/session/{sid}/screenshot",
            summary="[beat container] a small JPEG of what the page looks like")
async def screenshot(sid: str, x_assistant_token: str = Header(""),
                     x_beat_id: str = Header("")) -> dict:
    a, _ = await _caller(x_assistant_token, x_beat_id)
    _require_browser(a)
    _own(sid, a)
    data = await _pool("GET", f"/session/{sid}/screenshot", timeout=NAV_TIMEOUT_SEC)
    b64 = (data or {}).get("imageB64")
    if not b64:
        raise HTTPException(status_code=502, detail="the browser returned no image")
    import base64
    return {"data_uri": chrome._downscale_jpeg(base64.b64decode(b64), 640)}


@router.delete("/api/assistant/browser/session/{sid}",
               summary="[beat container] close one browser session")
async def close_one(sid: str, x_assistant_token: str = Header(""),
                    x_beat_id: str = Header("")) -> dict:
    a, _ = await _caller(x_assistant_token, x_beat_id)
    _own(sid, a)
    return {"ok": await close_session(sid)}


@router.post("/api/assistant/browser/close-all",
             summary="[beat container] close every session this beat opened")
async def close_all(x_assistant_token: str = Header(""),
                    x_beat_id: str = Header("")) -> dict:
    a, beat_id = await _caller(x_assistant_token, x_beat_id)
    if beat_id is not None:
        return {"closed": await close_beat_sessions(beat_id)}
    mine = [sid for sid, s in _sessions.items() if s.get("assistant_id") == int(a["id"])]
    for sid in mine:
        await close_session(sid)
    return {"closed": len(mine)}


# ---------------------------------------------------------------------------
# the self-test fixture — HTTP 200, and broken
# ---------------------------------------------------------------------------
# This page is the proof that a browser catches what curl cannot, and it is permanent
# regression cover for the tool itself. curl sees a 200 and markup that mentions a list of
# notes; a browser sees a TypeError and an EMPTY list, because the script reads `n.titel`
# (a typo) instead of `n.title` and dies before it appends anything. It also fires a request
# to a route that does not exist, so the failed-request collector has something real to
# catch. No auth: chrome-pool's browser has no token, and the page holds nothing.
_SELFTEST_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>builderapps browser self-test</title></head>
<body>
<h1>Notes</h1>
<p>This page is a deliberately broken fixture used to prove that an assistant's browser
catches faults that an HTTP 200 hides. It always returns 200.</p>
<ul id="notes"><li class="loading">Loading notes...</li></ul>
<button id="reload">Reload</button>
<script>
  var NOTES = [{title: "first note"}, {title: "second note"}];
  function render() {
    var ul = document.getElementById("notes");
    ul.innerHTML = "";
    NOTES.forEach(function (n) {
      // `titel` is a typo for `title` -> TypeError on undefined, and the list stays empty.
      ul.insertAdjacentHTML("beforeend", "<li>" + n.titel.toUpperCase() + "</li>");
    });
  }
  document.getElementById("reload").addEventListener("click", render);
  fetch("/api/assistant/browser/selftest-missing-endpoint").catch(function () {});
  render();
</script>
</body></html>
"""


@router.get(SELFTEST_PATH, response_class=HTMLResponse, include_in_schema=False,
            summary="A page that returns 200 and is broken in a browser (tool self-test)")
async def selftest() -> HTMLResponse:
    return HTMLResponse(content=_SELFTEST_HTML, status_code=200)
