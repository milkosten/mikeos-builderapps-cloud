"""Phase 34 — the OPEN-INTERNET reader the Discuss room browses with.

"Look at this page" / "read this PDF" during a discussion: the model calls a tool, this
module fetches the thing through the shared chrome-pool fleet, and a bounded digest of what
it actually said goes back into the conversation — with provenance, so the thread can show
`read example.com/article (1,240 words)` rather than the model quietly asserting facts.

╔══════════════════════════════════════════════════════════════════════════════════════╗
║ THIS POLICY IS DELIBERATELY THE OPPOSITE OF THE ASSISTANTS' — DO NOT "HARMONISE" THEM ║
╚══════════════════════════════════════════════════════════════════════════════════════╝
`server.browser_proxy` restricts an ASSISTANT container to its own project's URL and nothing
else: an autonomous agent with a browser on the open internet is a different risk entirely.
Discuss is the opposite by design — a human is present, they asked for a specific page, and
the whole point is to bring the outside world into the brief. Two different tools, two
different policies, on purpose. Whoever unifies them will either break Discuss or hand the
agents the internet.

Because it IS open, the guard here is the real one:

  * schemes: http/https only (no file:, data:, javascript:, gopher:, ftp:)
  * NEVER a private, loopback, link-local, reserved or multicast address — including cloud
    metadata at 169.254.169.254 — checked on the RESOLVED IP, not just the hostname, because
    `evil.example.com A 127.0.0.1` is the oldest trick in the SSRF book
  * never our own estate BY NAME: the whole `osmike.com` zone (control plane, gitea,
    chrome-pool, every tenant app on *.builderapps.osmike.com) and any dotless hostname,
    which on this box can only be a docker service name
  * the check is RE-RUN on the final URL after redirects — the classic bypass is a public
    host that 302s to 169.254.169.254
  * every fetch is logged with its verdict, so this is auditable after the fact

Everything is bounded: one page per call, a byte cap on the download path, and a character
cap on what reaches the model. A 200 KB article must cost a digest, not the conversation.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import httpx

from server.chrome import CHROME_POOL_URL, _auth, _close

logger = logging.getLogger(__name__)

# Bound EVERYTHING. These are the difference between "the model read a page" and "the model
# read a page and the next three turns cost ten times what they should".
MAX_DOWNLOAD_BYTES = int(os.environ.get("DISCUSS_WEB_MAX_BYTES", str(20 * 1024 * 1024)))
MAX_TEXT_CHARS = int(os.environ.get("DISCUSS_WEB_MAX_CHARS", "12000"))   # ~3k tokens
MAX_PDF_PAGES = int(os.environ.get("DISCUSS_WEB_MAX_PDF_PAGES", "40"))
FETCH_TIMEOUT = float(os.environ.get("DISCUSS_WEB_TIMEOUT", "60"))

# Our own estate, by name. The whole zone: telling an "internal" osmike.com host from a
# "public" one by its name is exactly the guess that eventually gets it wrong, and the cost
# of being wrong is the Discuss bot reading another tenant's app or the control plane's own
# API. Add a specific host to DISCUSS_WEB_ALLOW_HOSTS if a public one is ever genuinely needed.
BLOCKED_SUFFIXES = tuple(
    h.strip().lower() for h in os.environ.get(
        "DISCUSS_WEB_BLOCK_SUFFIXES",
        "osmike.com,localhost,local,internal,cluster.local,docker,arpa",
    ).split(",") if h.strip()
)
ALLOW_HOSTS = tuple(
    h.strip().lower() for h in os.environ.get("DISCUSS_WEB_ALLOW_HOSTS", "").split(",")
    if h.strip()
)
ALLOWED_SCHEMES = ("http", "https")


class Refused(Exception):
    """A blocked target. The message is shown to the user verbatim — say WHY."""


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------
def _ip_ok(ip: str) -> Tuple[bool, str]:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False, f"{ip} is not an IP address"
    if addr.is_loopback:
        return False, f"{ip} is a loopback address"
    if addr.is_link_local:
        # 169.254.0.0/16 — cloud metadata (169.254.169.254) lives here. Named explicitly
        # because it is THE target every SSRF write-up goes for.
        return False, f"{ip} is a link-local address (cloud metadata lives here)"
    if addr.is_private:
        return False, f"{ip} is a private address"
    if addr.is_reserved or addr.is_multicast or addr.is_unspecified:
        return False, f"{ip} is a reserved address"
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return _ip_ok(str(addr.ipv4_mapped))
    return True, ""


async def _resolve(host: str) -> List[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.run_in_executor(
        None, lambda: socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP))
    out, seen = [], set()
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


async def check_url(raw: str) -> str:
    """Return the normalized URL, or raise `Refused` with a human explanation.

    Both halves matter and neither is sufficient alone: the NAME check stops someone asking
    the bot to read `builderapps-api` or another tenant's app; the RESOLVED-IP check stops a
    public-looking name that points at 127.0.0.1 or the metadata service.
    """
    url = (raw or "").strip()
    if not url:
        raise Refused("no URL given")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    p = urlparse(url)
    scheme = (p.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise Refused(f"I can only open http:// and https:// links — `{scheme}:` is blocked.")
    host = (p.hostname or "").lower().strip(".")
    if not host:
        raise Refused("that URL has no hostname")

    if host in ALLOW_HOSTS:
        return urlunparse(p)

    # A hostname with no dot cannot be public. On this box it is a docker service name
    # (mikeos-builderapps, gitea, deploy-caddy-1, …) — i.e. our own control plane.
    if "." not in host:
        raise Refused(f"`{host}` is an internal service name, not a public website.")
    for suf in BLOCKED_SUFFIXES:
        if host == suf or host.endswith("." + suf):
            raise Refused(
                f"`{host}` is MikeOS' own infrastructure — I won't read our internal "
                "services or other people's apps. Give me a public page instead.")

    # A literal IP in the URL is checked directly; a name is resolved and EVERY answer checked.
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        try:
            ips = await _resolve(host)
        except Exception as e:  # noqa: BLE001
            raise Refused(f"I couldn't resolve `{host}` ({e}).") from e
    if not ips:
        raise Refused(f"`{host}` does not resolve to anything.")
    for ip in ips:
        ok, why = _ip_ok(ip)
        if not ok:
            # `why` already names the address. When the URL WAS the address, saying "X points
            # at X is a link-local address" reads like a bug; when it was a name, the
            # indirection is the whole point and has to be shown.
            if host == ip:
                raise Refused(f"`{host}` is {why.split(' is ', 1)[1]} — not a public website, "
                              "so I won't fetch it.")
            raise Refused(f"`{host}` resolves to {why} — not a public website, so I won't "
                          "fetch it.")
    return urlunparse(p)


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------
def _clean_text(s: str) -> str:
    s = re.sub(r"[ \t ]+", " ", s or "")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _digest(text: str) -> Tuple[str, int, bool]:
    """(text fed to the model, word count of the WHOLE thing, truncated?)."""
    text = _clean_text(text)
    words = len(text.split())
    if len(text) <= MAX_TEXT_CHARS:
        return text, words, False
    return text[:MAX_TEXT_CHARS].rsplit(" ", 1)[0], words, True


def _pdf_text(raw: bytes) -> str:
    from io import BytesIO
    try:
        from pypdf import PdfReader
    except ImportError as e:  # noqa: BLE001
        raise RuntimeError("no PDF extractor installed on this control plane") from e
    reader = PdfReader(BytesIO(raw))
    out = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        try:
            out.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 — one bad page must not lose the document
            continue
    return "\n\n".join(out)


def _looks_like_pdf(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


# ---------------------------------------------------------------------------
# the fetch
# ---------------------------------------------------------------------------
async def _read_page(url: str) -> Dict[str, Any]:
    """Navigate and take the ACCESSIBILITY-TREE SNAPSHOT — chrome-pool's `/snapshot`, which is
    the readable page rather than its HTML. One session, always closed with `DELETE
    /session/{id}` (there is no POST .../close; that 404s silently and leaks the session —
    swept estate-wide, do not reintroduce it)."""
    sid = None
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, verify=False, auth=_auth()) as c:
            r = await c.post(f"{CHROME_POOL_URL}/session")
            r.raise_for_status()
            sid = (r.json() or {}).get("sessionId") or (r.json() or {}).get("id")
            if not sid:
                raise RuntimeError("chrome-pool gave no session")
            nav = await c.post(f"{CHROME_POOL_URL}/session/{sid}/navigate",
                               json={"url": url, "acceptCookies": True})
            nav.raise_for_status()
            await asyncio.sleep(0.8)

            # REDIRECT RE-CHECK. A public host that 302s to 169.254.169.254 is the classic
            # bypass: the URL we validated is not necessarily the page we are now looking at.
            final = url
            try:
                ev = await c.post(f"{CHROME_POOL_URL}/session/{sid}/eval",
                                  json={"expression": "location.href"})
                final = str((ev.json() or {}).get("value") or url)
            except Exception:  # noqa: BLE001
                pass
            if final and final != url:
                await check_url(final)      # raises Refused, and the finally-block still closes

            title = ""
            try:
                tv = await c.post(f"{CHROME_POOL_URL}/session/{sid}/eval",
                                  json={"expression": "document.title"})
                title = str((tv.json() or {}).get("value") or "")[:200]
            except Exception:  # noqa: BLE001
                pass

            snap = await c.get(f"{CHROME_POOL_URL}/session/{sid}/snapshot")
            snap.raise_for_status()
            text = str((snap.json() or {}).get("text") or "")
            # NEVER TRUST 200 ALONE: a session that navigated nowhere returns a cheerful empty
            # snapshot, and reporting that as "read the page" is how a model ends up inventing
            # the contents of a page it never saw.
            if not text.strip():
                raise RuntimeError("the page rendered no readable text")
            return {"kind": "page", "final_url": final, "title": title, "text": text}
    finally:
        if sid:
            await _close(sid)


async def _read_pdf(url: str) -> Dict[str, Any]:
    """chrome-pool's `POST /download` (SSRF-guarded on their side too, ~25 MB) + server-side
    text extraction. Chrome renders a PDF in a viewer whose DOM says nothing useful, so the
    snapshot path is worthless here — this is why PDFs get their own route."""
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, verify=False, auth=_auth()) as c:
        r = await c.post(f"{CHROME_POOL_URL}/download", json={"url": url})
        r.raise_for_status()
        data = r.json() or {}
    b64 = data.get("contentB64") or data.get("dataB64") or data.get("base64") or ""
    if not b64:
        raise RuntimeError("the download returned no content")
    import base64
    raw = base64.b64decode(b64)
    if len(raw) > MAX_DOWNLOAD_BYTES:
        raise RuntimeError(f"that file is {len(raw) // (1024 * 1024)} MB — too big to read here")
    text = _pdf_text(raw)
    if not text.strip():
        # SAY SO. A scanned PDF has no text layer, and "I read it" followed by invention is
        # the single worst failure this feature could have.
        raise RuntimeError("I downloaded the PDF but it has no extractable text (it is "
                           "probably a scan — there is no text layer to read)")
    return {"kind": "pdf", "final_url": url, "title": urlparse(url).path.rsplit("/", 1)[-1],
            "text": text}


async def read(raw_url: str) -> Dict[str, Any]:
    """The one entry point. NEVER raises: returns {ok, url, title, kind, text, words,
    truncated, error} so a refusal or a dead site becomes something the model (and the thread)
    can talk about honestly instead of an exception that loses the turn."""
    try:
        url = await check_url(raw_url)
    except Refused as e:
        logger.warning("discuss webread REFUSED %r: %s", raw_url, e)
        return {"ok": False, "url": raw_url, "error": str(e), "refused": True,
                "title": "", "kind": "", "text": "", "words": 0}

    try:
        if _looks_like_pdf(url):
            got = await _read_pdf(url)
        else:
            got = await _read_page(url)
    except Refused as e:
        logger.warning("discuss webread REFUSED after redirect %r: %s", url, e)
        return {"ok": False, "url": url, "error": str(e), "refused": True,
                "title": "", "kind": "", "text": "", "words": 0}
    except Exception as e:  # noqa: BLE001
        logger.info("discuss webread FAILED %s: %s", url, e)
        return {"ok": False, "url": url, "error": str(e)[:300], "refused": False,
                "title": "", "kind": "", "text": "", "words": 0}

    text, words, truncated = _digest(got.get("text") or "")
    logger.info("discuss webread OK %s (%s, %d words%s)",
                got.get("final_url") or url, got.get("kind"), words,
                ", truncated" if truncated else "")
    return {"ok": True, "url": got.get("final_url") or url, "title": got.get("title") or "",
            "kind": got.get("kind") or "page", "text": text, "words": words,
            "truncated": truncated, "refused": False, "error": ""}


def short_host(url: str) -> str:
    try:
        p = urlparse(url)
        path = (p.path or "").rstrip("/")
        return (p.hostname or url) + (path if len(path) <= 40 else path[:37] + "…")
    except Exception:  # noqa: BLE001
        return url[:60]
