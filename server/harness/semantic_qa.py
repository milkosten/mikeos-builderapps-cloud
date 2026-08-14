"""Semantic QA (phase 28) — seed a record through the app's own API, then prove it RENDERS.

The failure this exists to catch actually happened: runtime QA passed a link shortener while
its list rendered "No links yet" **with rows sitting in Postgres**. Nothing was wrong that the
old QA could see — the page loaded, the console was empty, no request 4xx'd. The client had
caught its own `links.forEach is not a function` and rendered the empty state as *text*, so
`window.__errs` was empty and QA called it clean.

Two lessons are baked in here:

1. **QA cannot know there SHOULD be data.** A freshly-built app's list is legitimately empty,
   so "the list is empty" is not a signal — unless QA itself put a row there. So we seed one,
   through the app's own public API, with a unique marker string.
2. **Assert on rendered CONTENT, not on the absence of errors.** A caught error rendered as
   text produces a perfectly clean console. The assertion is `the marker appears in
   document.body.innerText`.

The three-way outcome is the whole value — it localizes the bug for the repair pass:

| seed write | API read-back | rendered | verdict |
|---|---|---|---|
| fails | — | — | the WRITE path is broken (bad column, 422, 500) |
| ok | marker absent | — | the READ endpoint does not return what it stored |
| ok | marker present | marker absent | **the FRONTEND is not rendering data the API returns** |
| ok | ok | ok | the flow genuinely works |

Flows come from `docs/UX.md` + the app's real routes, so QA tests what the app was *meant* to
do rather than a generic guess. Everything is best-effort about capturing and strict about
reporting: an inconclusive probe is never reported as a pass.

**The seed and the render share ONE browser session.** Seeding over a server-side HTTP client
put the app's session cookie in that client's jar, and the render check then opened a fresh,
logged-OUT browser — so on any app with a login every flow "failed" and QA cried wolf (three
of four measured builds). The seed is now a `fetch(..., credentials:'same-origin')` from
inside the page, so the render sees exactly the session a real user would.
"""
import asyncio
import json
import logging
import re
import secrets
from typing import Any, Dict, List, Optional

import httpx

from server import chrome, gpu, introspect, workspace
from server.harness import codegen

logger = logging.getLogger(__name__)

MAX_FLOWS = 3                 # keep QA bounded — 3 seeded flows is plenty of signal
HTTP_TIMEOUT = 25.0
RENDER_ATTEMPTS = 3           # the page may fetch its list asynchronously
MARKER_PREFIX = "QASEED"


def _marker() -> str:
    """A unique, obviously-synthetic, single-token string that must survive into the DOM."""
    return f"{MARKER_PREFIX}{secrets.token_hex(4).upper()}"


def _inject(value: Any, marker: str) -> Any:
    """Replace the literal `__MARKER__` placeholder anywhere in the planned request body."""
    if isinstance(value, str):
        return value.replace("__MARKER__", marker)
    if isinstance(value, list):
        return [_inject(v, marker) for v in value]
    if isinstance(value, dict):
        return {k: _inject(v, marker) for k, v in value.items()}
    return value


_PLAN_SYS = (
    "You design END-TO-END smoke tests for a small self-hosted Node+Express+Postgres web app. "
    "You are given the app's UX doc and its REAL routes. Design up to "
    f"{MAX_FLOWS} flows that each: create one record through a write endpoint, then verify it "
    "shows up on a page.\n\n"
    "Rules:\n"
    "- Use ONLY routes from the list you are given. Never invent one.\n"
    "- `create.body` must be a complete, VALID body for that endpoint: every required field "
    "present, correct JSON types (numbers as numbers, never \"\" for an integer column), and "
    "realistic values. Where the app expects a URL, use a real absolute one like "
    "https://example.com/__MARKER__ .\n"
    "- Put the literal token __MARKER__ inside exactly one short TEXT field whose value a user "
    "would SEE on the page (a title, name, note body, label). That token is how we detect the "
    "record in the rendered page, so it must not be a field the UI hides.\n"
    "- `list` is the GET endpoint that returns the collection you just wrote to (no params).\n"
    "- `page` is the frontend path where that record should become visible, usually \"/\". "
    "If it is a detail page for the record you just created, write the id as `:id` "
    "(e.g. \"/links/:id\") — the id of the created record is substituted in for you.\n"
    "Respond ONLY as JSON:\n"
    '{"flows":[{"name":"...","create":{"method":"POST","path":"/api/x","body":{...}},'
    '"list":"/api/x","page":"/"}]}'
)


async def plan_flows(project_id: str, brief: str, tech_plan: str) -> List[Dict[str, Any]]:
    """Derive the seed-and-assert flows from docs/UX.md + the app's actual routes."""
    routes = introspect.routes(project_id)
    if not routes:
        return []
    writes = [r for r in routes if r["method"] in ("POST", "PUT", "PATCH")]
    if not writes:
        return []                      # a read-only app has nothing to seed
    try:
        ux = workspace.read_file_capped(project_id, "docs/UX.md") or ""
    except Exception:  # noqa: BLE001
        ux = ""
    route_lines = "\n".join(f"{r['method']} {r['path']}" for r in routes[:60])
    user = (
        f"App brief: {brief}\n\n"
        f"User experience doc (the flows a tester should be able to click through):\n"
        f"{ux[:4000] or '(none)'}\n\n"
        f"Technical plan (data model + routes):\n{tech_plan[:3000]}\n\n"
        f"The app's REAL routes:\n{route_lines}\n\n"
        f"Design up to {MAX_FLOWS} seed-and-verify flows."
    )
    reply = await gpu.chat(
        [{"role": "system", "content": _PLAN_SYS + "\n\n" + codegen.NO_SAAS_RULE},
         {"role": "user", "content": user}],
        schema={"type": "object"}, temperature=0.2, num_predict=1200, timeout=240)
    try:
        data = codegen._extract_json(reply)
    except Exception as e:  # noqa: BLE001 — a bad plan must never break QA
        logger.info("semantic QA plan unparseable for %s: %s", project_id, e)
        return []
    flows = data.get("flows") if isinstance(data, dict) else None
    out: List[Dict[str, Any]] = []
    known = {(r["method"], r["path"]) for r in routes}
    for f in (flows or [])[:MAX_FLOWS]:
        if not isinstance(f, dict):
            continue
        create = f.get("create") or {}
        method = str(create.get("method") or "POST").upper()
        path = str(create.get("path") or "")
        body = create.get("body")
        if not path.startswith("/") or not isinstance(body, dict):
            continue
        # Only trust a route the app really has (allow a :param route to match by shape).
        if (method, path) not in known and not any(
                m == method and _same_route(p, path) for m, p in known):
            logger.info("semantic QA: dropping invented route %s %s", method, path)
            continue
        if "__MARKER__" not in json.dumps(body):
            continue                   # no observable field -> the assertion would be vacuous
        out.append({
            "name": str(f.get("name") or path)[:120],
            "method": method, "path": path, "body": body,
            "list": str(f.get("list") or "") or None,
            "page": str(f.get("page") or "/") or "/",
        })
    return out


_PLACEHOLDER_RE = re.compile(r":[A-Za-z_]\w*|\{[A-Za-z_]\w*\}|__ID__")


def _resolve_page(page: str, create_response: str) -> str:
    """Substitute the id of the record we just created into a detail-page path.

    The planner is allowed to answer `page: "/links/:id"` — the natural place to look for a
    freshly created record. Nothing used to fill `:id` in, so the browser was sent to the
    literal path, the app answered "not found", and QA reported a *broken* app that was
    perfectly healthy (measured on the campaign's first build). `_ID_RE` was written for this
    and then never wired up.

    If the create response carries no id we fall back to the index rather than assert against
    a URL we know is wrong — a weaker assertion beats a false accusation.
    """
    if not _PLACEHOLDER_RE.search(page or ""):
        return page or "/"
    m = _ID_RE.search(create_response or "")
    if not m:
        return "/"
    return _PLACEHOLDER_RE.sub(m.group(2), page)


def _same_route(template: str, concrete: str) -> bool:
    """`/api/notes/:id` matches `/api/notes/123` (segment count + literal segments)."""
    a, b = template.strip("/").split("/"), concrete.strip("/").split("/")
    if len(a) != len(b):
        return False
    return all(seg.startswith(":") or seg == other for seg, other in zip(a, b))


class Browser:
    """One chrome-pool session used for BOTH the seed and the render check.

    This has to be the same session, or the check is meaningless on any app with a login:
    seeding over a server-side HTTP client puts the session cookie in *that* client's jar,
    then rendering in a fresh browser shows a logged-OUT page and every flow "fails". The
    seed therefore runs as `fetch(..., credentials:'same-origin')` from inside the page, so
    whatever cookie the app sets is the browser's own and the render sees the same session a
    real user would.

    `fetch` is async and chrome-pool's /eval is synchronous, so a request is kicked off into
    `window.__qa_seed` and then polled.
    """

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.sid: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "Browser":
        self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=False,
                                         auth=(chrome.CHROME_POOL_USER, chrome.CHROME_POOL_PASS))
        r = await self._client.post(f"{chrome.CHROME_POOL_URL}/session")
        r.raise_for_status()
        j = r.json() or {}
        self.sid = j.get("sessionId") or j.get("id")
        if not self.sid:
            raise RuntimeError("chrome-pool gave no session id")
        await self.goto("/")
        return self

    async def __aexit__(self, *exc) -> bool:
        try:
            if self.sid and self._client:
                await self._client.post(f"{chrome.CHROME_POOL_URL}/session/{self.sid}/close")
        except Exception:  # noqa: BLE001
            pass
        if self._client:
            await self._client.aclose()
        return False

    async def _eval(self, expression: str) -> Any:
        r = await self._client.post(f"{chrome.CHROME_POOL_URL}/session/{self.sid}/eval",
                                    json={"expression": expression})
        return (r.json() or {}).get("value")

    async def goto(self, path: str) -> None:
        await self._client.post(f"{chrome.CHROME_POOL_URL}/session/{self.sid}/navigate",
                                json={"url": self.base + (path or "/"), "acceptCookies": True})
        await asyncio.sleep(1.2)

    async def request(self, method: str, path: str,
                      body: Optional[dict] = None) -> tuple[int, str]:
        """Issue an API call FROM THE PAGE, carrying the browser's own cookies."""
        opts = {"method": method, "credentials": "same-origin",
                "headers": {"Content-Type": "application/json"}}
        if body is not None:
            opts["body"] = json.dumps(body)
        kick = ("(function(){window.__qa_seed=null;"
                f"fetch({json.dumps(path)},{json.dumps(opts)})"
                ".then(function(r){return r.text().then(function(t){"
                "window.__qa_seed={status:r.status,text:t.slice(0,4000)};});})"
                ".catch(function(e){window.__qa_seed={status:0,text:'fetch failed: '+e};});"
                "return 'sent';})()")
        try:
            await self._eval(kick)
            for _ in range(20):                    # up to ~10s
                await asyncio.sleep(0.5)
                raw = await self._eval("JSON.stringify(window.__qa_seed)")
                if isinstance(raw, str) and raw and raw != "null":
                    got = json.loads(raw)
                    return int(got.get("status") or 0), str(got.get("text") or "")
            return 0, "request timed out in the browser"
        except Exception as e:  # noqa: BLE001
            return 0, f"request failed: {e}"

    async def page_text(self, path: str) -> str:
        """The page's visible text, retried — a list is usually fetched after load."""
        await self.goto(path)
        for attempt in range(RENDER_ATTEMPTS):
            text = await self._eval(
                "(document.body && document.body.innerText || '').slice(0,20000)")
            if isinstance(text, str) and text.strip():
                return text
            await asyncio.sleep(1.5 * (attempt + 1))
        return ""


browser_factory = Browser        # swapped out by the offline tests


async def run_flows(project_id: str, base_url: str, flows: List[Dict[str, Any]]
                    ) -> Dict[str, Any]:
    """Execute the seed-and-assert flows against the LIVE app.

    Returns {"findings": [str, ...], "checked": int, "passed": int, "detail": [...]}.
    A finding is a sentence written for the repair agent, naming the route, the marker and
    exactly which of the three links in the chain broke.
    """
    result: Dict[str, Any] = {"findings": [], "checked": 0, "passed": 0, "detail": []}
    async with browser_factory(base_url) as br:
        for flow in flows:
            marker = _marker()
            body = _inject(flow["body"], marker)
            result["checked"] += 1
            status, text = await br.request(flow["method"], flow["path"], body)
            step = {"flow": flow["name"], "marker": marker, "create_status": status}

            # 1. the WRITE path — never trust 200 alone: the record must come back with an id
            if not (200 <= status < 300):
                result["findings"].append(
                    f"FLOW '{flow['name']}': creating a record failed — "
                    f"{flow['method']} {flow['path']} returned HTTP {status}. "
                    f"Request body was {json.dumps(body)[:300]}. Response: {text[:300]}")
                result["detail"].append({**step, "verdict": "write_failed"})
                continue

            # 2. the READ path — does the app's own list endpoint return it back?
            list_path = flow.get("list")
            api_has = None
            if list_path:
                lstatus, ltext = await br.request("GET", list_path)
                if 200 <= lstatus < 300:
                    api_has = marker in ltext
                    if not api_has:
                        result["findings"].append(
                            f"FLOW '{flow['name']}': {flow['method']} {flow['path']} accepted "
                            f"the record (HTTP {status}) but GET {list_path} does NOT return "
                            f"it (marker {marker} absent from the response). The write is "
                            f"being dropped, filtered out, or read back from the wrong table "
                            f"/column. Response was: {ltext[:300]}")
                        result["detail"].append({**step, "verdict": "read_missing"})
                        continue
                else:
                    result["findings"].append(
                        f"FLOW '{flow['name']}': GET {list_path} returned HTTP {lstatus} — "
                        f"the page cannot list what it just created. Response: {ltext[:300]}")
                    result["detail"].append({**step, "verdict": "list_failed"})
                    continue

            # 3. the RENDER — the only assertion that catches "No links yet" with rows in the DB
            page = _resolve_page(flow.get("page") or "/", text)
            step["page"] = page
            shown = await br.page_text(page)
            if not shown:
                result["detail"].append({**step, "verdict": "page_unreadable"})
                result["findings"].append(
                    f"FLOW '{flow['name']}': the page {page} rendered no "
                    f"readable text at all — it is blank or the JS threw before painting.")
                continue
            if marker in shown:
                result["passed"] += 1
                result["detail"].append({**step, "verdict": "ok"})
                continue
            api_note = ("and GET %s DOES return it" % list_path if api_has
                        else "(no list endpoint to cross-check)")
            result["findings"].append(
                f"FLOW '{flow['name']}': a record was created via {flow['method']} "
                f"{flow['path']} (HTTP {status}) {api_note}, but it does NOT appear anywhere "
                f"in the rendered page {page} — the marker {marker} is "
                f"absent from the page text. The FRONTEND is not rendering data the API "
                f"returns: read what that endpoint actually returns and unwrap that exact key "
                f"in the client before iterating. The page currently shows: "
                f"{shown[:300]!r}")
            result["detail"].append({**step, "verdict": "not_rendered"})
    return result


_ID_RE = re.compile(r'"id"\s*:\s*("?)([A-Za-z0-9_-]{1,64})\1')


async def run(project_id: str, *, brief: str, tech_plan: str, base_url: str,
              flows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Plan (once) + execute the semantic flows. Never raises — QA must not break a build."""
    try:
        flows = flows if flows is not None else await plan_flows(project_id, brief, tech_plan)
        if not flows:
            return {"findings": [], "checked": 0, "passed": 0, "detail": [], "flows": []}
        out = await run_flows(project_id, base_url, flows)
        out["flows"] = flows
        return out
    except Exception as e:  # noqa: BLE001
        logger.info("semantic QA skipped for %s: %s", project_id, e)
        return {"findings": [], "checked": 0, "passed": 0, "detail": [], "flows": [],
                "error": str(e)[:300]}
