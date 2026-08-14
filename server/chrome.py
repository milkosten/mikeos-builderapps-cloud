"""chrome-pool helpers — thumbnails + best-effort hero images for generated sites.

Uses the shared headless-Chrome fleet (`CHROME_POOL_URL`, HTTP Basic) to:
  * screenshot a hosted site `/<id>/` -> a small JPEG data: URI (project thumbnail), and
  * download one free stock image -> a data: URI the harness may inline as a hero.

Everything here is BEST-EFFORT and degrades gracefully: any failure returns None so it can
never break generation or the request. Never trust HTTP 200 alone — we verify the payload
actually decodes to bytes/an image before using it.
"""
import asyncio
import base64
import json
import logging
import os
from typing import Any, List, Optional

import httpx

logger = logging.getLogger(__name__)

CHROME_POOL_URL = os.environ.get("CHROME_POOL_URL", "https://chrome-pool.osmike.com").rstrip("/")
CHROME_POOL_USER = os.environ.get("CHROME_POOL_USER", "mikeos")
CHROME_POOL_PASS = os.environ.get("CHROME_POOL_PASS", "uB49VXwMDy7R2JE0H7mI")

_TIMEOUT = float(os.environ.get("CHROME_POOL_TIMEOUT", "45"))


def _auth() -> tuple[str, str]:
    return (CHROME_POOL_USER, CHROME_POOL_PASS)


async def _close(sid: str) -> bool:
    """Close a chrome-pool session. **`DELETE /session/{id}` — there is no `.../close`.**

    Every caller in this file used to `POST /session/{sid}/close`, which chrome-pool answers
    with a 404 ("Cannot POST"), inside a bare `except: pass`. So no session this platform ever
    opened was actually closed: they all sat there until chrome-pool's own 30-minute TTL
    collected them, and the shared pool was found holding 11 live sessions with nobody using
    it. A cleanup that silently does nothing is worse than no cleanup, because it stops
    anyone from looking.
    """
    if not sid:
        return False
    try:
        async with httpx.AsyncClient(timeout=15, verify=False, auth=_auth()) as c:
            r = await c.delete(f"{CHROME_POOL_URL}/session/{sid}")
        if r.status_code >= 400 and r.status_code != 404:   # 404 = already gone; fine
            logger.info("chrome-pool: close of %s returned %s", sid, r.status_code)
            return False
        return True
    except Exception as e:  # noqa: BLE001 — a failed close must never raise into a caller
        logger.info("chrome-pool: could not close session %s: %s", sid, e)
        return False


def _downscale_jpeg(png_or_jpeg: bytes, width: int = 320) -> Optional[str]:
    """Downscale image bytes to a small JPEG data: URI. Falls back to the raw bytes as a
    data: URI (best-guess mime) if Pillow is unavailable."""
    try:
        import io
        from PIL import Image  # optional dependency
        im = Image.open(io.BytesIO(png_or_jpeg)).convert("RGB")
        if im.width > width:
            h = max(1, int(im.height * (width / im.width)))
            im = im.resize((width, h), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=72, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/jpeg;base64,{b64}"
    except Exception as e:  # noqa: BLE001
        logger.info("thumbnail downscale skipped (%s); storing raw", e)
        mime = "image/png" if png_or_jpeg[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(png_or_jpeg).decode()}"


async def screenshot_data_uri(url: str, width: int = 320) -> Optional[str]:
    """Screenshot `url` via chrome-pool and return a small JPEG data: URI (or None)."""
    sid = None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=False, auth=_auth()) as client:
            r = await client.post(f"{CHROME_POOL_URL}/session")
            r.raise_for_status()
            sid = (r.json() or {}).get("id") or (r.json() or {}).get("sessionId")
            if not sid:
                logger.info("chrome-pool: no session id in response")
                return None
            await client.post(f"{CHROME_POOL_URL}/session/{sid}/navigate",
                              json={"url": url, "acceptCookies": True})
            shot = await client.get(f"{CHROME_POOL_URL}/session/{sid}/screenshot")
            shot.raise_for_status()
            data = shot.json() or {}
            b64 = data.get("imageB64") or data.get("image") or data.get("data")
            if not b64:
                logger.info("chrome-pool: no imageB64 in screenshot response")
                return None
            try:
                raw = base64.b64decode(b64)
            except Exception:
                return None
            if not raw:
                return None
            return _downscale_jpeg(raw, width)
    except Exception as e:  # noqa: BLE001
        logger.info("thumbnail screenshot failed for %s: %s", url, e)
        return None
    finally:
        if sid:
            await _close(sid)


async def fetch_image_data_uri(keywords: str, w: int = 1200, h: int = 630,
                               max_width: int = 1200) -> Optional[str]:
    """Best-effort: fetch ONE relevant free stock image (Unsplash Source) via chrome-pool's
    SSRF-guarded /download and return it as a data: URI (downscaled). None on any failure."""
    kw = "-".join((keywords or "").split())[:80] or "abstract"
    url = f"https://source.unsplash.com/{w}x{h}/?{kw}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=False, auth=_auth()) as client:
            r = await client.post(f"{CHROME_POOL_URL}/download", json={"url": url})
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            if "application/json" in ct:
                data = r.json() or {}
                b64 = data.get("data") or data.get("imageB64") or data.get("bytes")
                raw = base64.b64decode(b64) if b64 else b""
            else:
                raw = r.content
            if not raw or len(raw) < 256:
                return None
            return _downscale_jpeg(raw, max_width)
    except Exception as e:  # noqa: BLE001
        logger.info("hero image fetch failed for '%s': %s", keywords, e)
        return None


# ---- runtime QA: capture JS console errors --------------------------------
# chrome-pool has NO console-log endpoint, so we inject a tiny error collector into a
# COPY of the page (see harness.runtime_qa) that records every window `error` /
# `unhandledrejection` / `console.error(...)` into `window.__errs`, then read that array
# back via /eval after loading + exercising the UI.
ERR_COLLECTOR = (
    '<script>window.__errs=[];'
    'addEventListener("error",function(e){window.__errs.push((e.message||"error")+" @"+'
    '(e.filename||"").split("/").pop()+":"+(e.lineno||0));});'
    'addEventListener("unhandledrejection",function(e){window.__errs.push('
    '"unhandledrejection: "+((e.reason&&e.reason.message)||e.reason));});'
    'var _e=console.error;console.error=function(){window.__errs.push('
    '"console.error: "+Array.prototype.join.call(arguments," "));'
    '_e.apply(console,arguments);};</script>'
)

# JS that exercises the page: click up to ~10 buttons (never <a>, which would navigate
# away) and fire a few keyboard events, then report how many it touched. Wrapped in an
# IIFE so /eval gets a single value.
_EXERCISE_JS = (
    "(function(){var n=0;try{"
    "var b=document.querySelectorAll('button,[role=button]');"
    "for(var i=0;i<b.length&&n<10;i++){try{b[i].click();n++;}catch(e){}}"
    "var ks=['ArrowUp','ArrowDown','ArrowLeft','ArrowRight',' ','Enter'];"
    "for(var j=0;j<ks.length;j++){try{"
    "document.dispatchEvent(new KeyboardEvent('keydown',{key:ks[j],bubbles:true}));"
    "document.dispatchEvent(new KeyboardEvent('keyup',{key:ks[j],bubbles:true}));"
    "}catch(e){}}"
    "}catch(e){}return n;})()"
)


def inject_collector(html: str) -> str:
    """Return a COPY of `html` with the error collector inserted so it runs BEFORE the
    page's own scripts (right after <head>, else prepended)."""
    if not html:
        return ERR_COLLECTOR
    m = None
    low = html.lower()
    idx = low.find("<head>")
    if idx != -1:
        cut = idx + len("<head>")
        return html[:cut] + ERR_COLLECTOR + html[cut:]
    # no <head> — try after <head ...>
    import re as _re
    m = _re.search(r"<head[^>]*>", html, _re.IGNORECASE)
    if m:
        cut = m.end()
        return html[:cut] + ERR_COLLECTOR + html[cut:]
    return ERR_COLLECTOR + html


# A collector installed via /eval RIGHT AFTER navigate (window state does NOT survive a
# navigate on chrome-pool, so we can't pre-inject). It wraps fetch + XMLHttpRequest to record
# 4xx/5xx + network failures, and adds error/unhandledrejection listeners. It therefore catches
# everything that happens from interactions AFTER load (form submits, button clicks, XHR) — the
# faults that matter most. Pure load-time throws are additionally covered by tailing app logs.
_INSTALL_COLLECTORS_JS = (
    "(function(){"
    "if(window.__qa)return 'already';window.__qa=1;window.__errs=[];window.__net=[];"
    "addEventListener('error',function(e){try{window.__errs.push((e.message||'error')+' @'+"
    "String(e.filename||'').split('/').pop()+':'+(e.lineno||0));}catch(_){}});"
    "addEventListener('unhandledrejection',function(e){try{window.__errs.push("
    "'unhandledrejection: '+((e.reason&&e.reason.message)||e.reason));}catch(_){}});"
    "var _ce=console.error;console.error=function(){try{window.__errs.push("
    "'console.error: '+Array.prototype.join.call(arguments,' '));}catch(_){}"
    "_ce.apply(console,arguments);};"
    "var _f=window.fetch;if(_f){window.fetch=function(){var args=arguments;var u=args[0]&&args[0].url||args[0];"
    "return _f.apply(this,args).then(function(r){if(!r.ok){window.__net.push(r.status+' '+String(u));}return r;},"
    "function(err){window.__net.push('FETCH_FAIL '+String(u)+' '+(err&&err.message||err));throw err;});};}"
    "var _open=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){this.__u=u;"
    "return _open.apply(this,arguments);};"
    "var _send=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.send=function(){var x=this;"
    "x.addEventListener('load',function(){if(x.status>=400){window.__net.push(x.status+' '+String(x.__u));}});"
    "x.addEventListener('error',function(){window.__net.push('XHR_FAIL '+String(x.__u));});"
    "return _send.apply(this,arguments);};"
    "return 'installed';})()"
)


async def qa_run(url: str, exercise: bool = True,
                 timeout: Optional[float] = None) -> dict:
    """Full runtime-QA pass against a LIVE url in one chrome-pool session.

    Navigates, installs error+network collectors, exercises the UI, then reads back JS console
    errors + failed network requests + a screenshot data: URI. Best-effort: returns a dict with
    empty lists on any chrome-pool failure so QA can never break generation.

    Returns {errors:[...], network:[...], screenshot: data-uri|None, ok: bool}.
    """
    to = float(timeout if timeout is not None else max(_TIMEOUT, 60.0))
    sid = None
    result = {"errors": [], "network": [], "screenshot": None, "ok": False}
    try:
        async with httpx.AsyncClient(timeout=to, verify=False, auth=_auth()) as client:
            r = await client.post(f"{CHROME_POOL_URL}/session")
            r.raise_for_status()
            j = r.json() or {}
            sid = j.get("sessionId") or j.get("id")
            if not sid:
                return result
            await client.post(f"{CHROME_POOL_URL}/session/{sid}/navigate",
                              json={"url": url, "acceptCookies": True})
            await asyncio.sleep(1.0)
            await client.post(f"{CHROME_POOL_URL}/session/{sid}/eval",
                              json={"expression": _INSTALL_COLLECTORS_JS})
            if exercise:
                try:
                    await client.post(f"{CHROME_POOL_URL}/session/{sid}/eval",
                                      json={"expression": _EXERCISE_JS})
                    await asyncio.sleep(1.5)  # let click/submit handlers + their XHRs run
                except Exception as e:  # noqa: BLE001
                    logger.info("qa exercise failed for %s: %s", url, e)
            # read collectors
            re_ = await client.post(f"{CHROME_POOL_URL}/session/{sid}/eval",
                                    json={"expression": "JSON.stringify(window.__errs||[])"})
            rn_ = await client.post(f"{CHROME_POOL_URL}/session/{sid}/eval",
                                    json={"expression": "JSON.stringify(window.__net||[])"})
            result["errors"] = _parse_json_list((re_.json() or {}).get("value"))
            result["network"] = _parse_json_list((rn_.json() or {}).get("value"))
            try:
                shot = await client.get(f"{CHROME_POOL_URL}/session/{sid}/screenshot")
                b64 = (shot.json() or {}).get("imageB64")
                if b64:
                    raw = base64.b64decode(b64)
                    result["screenshot"] = _downscale_jpeg(raw, 640)
            except Exception:
                pass
            result["ok"] = True
            return result
    except Exception as e:  # noqa: BLE001
        logger.info("qa_run failed for %s: %s", url, e)
        return result
    finally:
        if sid:
            await _close(sid)


def _parse_json_list(val) -> List[str]:
    if isinstance(val, str) and val.strip():
        try:
            p = json.loads(val)
            if isinstance(p, list):
                # unique, order-preserving
                seen, out = set(), []
                for x in p:
                    s = str(x)
                    if s and s not in seen:
                        seen.add(s)
                        out.append(s)
                return out
        except Exception:
            pass
    return []


async def eval_js(url: str, expression: str, navigate: bool = True,
                  timeout: Optional[float] = None) -> Optional[Any]:
    """Navigate (optional) and evaluate a JS expression, returning its `value` (or None).
    Used by QA to assert real UX behavior (e.g. count rendered rows) — never-trust-200 for UX."""
    to = float(timeout if timeout is not None else _TIMEOUT)
    sid = None
    try:
        async with httpx.AsyncClient(timeout=to, verify=False, auth=_auth()) as client:
            r = await client.post(f"{CHROME_POOL_URL}/session")
            r.raise_for_status()
            j = r.json() or {}
            sid = j.get("sessionId") or j.get("id")
            if not sid:
                return None
            if navigate:
                await client.post(f"{CHROME_POOL_URL}/session/{sid}/navigate",
                                  json={"url": url, "acceptCookies": True})
                await asyncio.sleep(1.2)
            rr = await client.post(f"{CHROME_POOL_URL}/session/{sid}/eval",
                                   json={"expression": expression})
            return (rr.json() or {}).get("value")
    except Exception as e:  # noqa: BLE001
        logger.info("eval_js failed for %s: %s", url, e)
        return None
    finally:
        if sid:
            await _close(sid)


async def console_errors(url: str, exercise: bool = True,
                         timeout: Optional[float] = None) -> List[str]:
    """Load `url` in a real headless browser, optionally exercise the UI, and return the
    unique JS console errors it produced (from the injected `window.__errs`).

    The page at `url` MUST already contain the ERR_COLLECTOR (inject it into a temp copy
    first — see harness.runtime_qa). Best-effort: returns [] on any chrome-pool failure so
    QA can never break a generation. Bounded by `timeout` (per-nav)."""
    to = float(timeout if timeout is not None else _TIMEOUT)
    sid = None
    errs: List[str] = []
    try:
        async with httpx.AsyncClient(timeout=to, verify=False, auth=_auth()) as client:
            r = await client.post(f"{CHROME_POOL_URL}/session")
            r.raise_for_status()
            j = r.json() or {}
            sid = j.get("sessionId") or j.get("id")
            if not sid:
                logger.info("chrome-pool QA: no session id")
                return []
            await client.post(f"{CHROME_POOL_URL}/session/{sid}/navigate",
                              json={"url": url, "acceptCookies": True})
            await asyncio.sleep(1.5)   # let the page's scripts run / throw
            if exercise:
                try:
                    await client.post(f"{CHROME_POOL_URL}/session/{sid}/eval",
                                      json={"expression": _EXERCISE_JS})
                except Exception as e:  # noqa: BLE001 — exercising is optional
                    logger.info("chrome-pool QA exercise failed for %s: %s", url, e)
                await asyncio.sleep(1.0)   # let click/key handlers throw
            r = await client.post(f"{CHROME_POOL_URL}/session/{sid}/eval",
                                  json={"expression": "JSON.stringify(window.__errs||[])"})
            r.raise_for_status()
            val = (r.json() or {}).get("value")
            if isinstance(val, str) and val.strip():
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, list):
                        errs = [str(x) for x in parsed if x]
                except Exception:
                    pass
    except Exception as e:  # noqa: BLE001
        logger.info("chrome-pool console_errors failed for %s: %s", url, e)
        return []
    finally:
        if sid:
            await _close(sid)
    # unique, order-preserving
    seen = set()
    uniq: List[str] = []
    for e in errs:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq
