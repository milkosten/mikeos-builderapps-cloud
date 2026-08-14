"""Offline tests for semantic QA (phase 28).

Replays the exact bug that slipped through the old QA — the link shortener that rendered
"No links yet" while rows sat in Postgres — with a FAKE app (httpx + chrome monkeypatched),
and proves the three-way verdict localizes each break:

    write fails      -> "creating a record failed"
    read-back misses -> "accepted the record but GET ... does not return it"
    not rendered     -> "the FRONTEND is not rendering data the API returns"   <- the real bug
    all three ok     -> pass

Also proves the planner refuses invented routes and vacuous (marker-less) flows.

    python3 -m tests.test_semantic_qa
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="semqatest-")
os.environ["WORKSPACES_ROOT"] = os.path.join(_TMP, "workspaces")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import chrome, gpu, introspect  # noqa: E402
from server.harness import semantic_qa  # noqa: E402

PROJECT = "sem001"
WS = os.path.join(_TMP, "workspaces", PROJECT)

_passed = 0
_failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed.append(name)
        print(f"  FAIL {name}  {detail}")


# ---- a fake live app -------------------------------------------------------
class FakeResponse:
    def __init__(self, status: int, text: str):
        self.status_code = status
        self.text = text


class FakeApp:
    """Simulates the app behind the public URL. `mode` picks which link in the chain breaks."""

    def __init__(self, mode: str):
        self.mode = mode
        self.rows: list[str] = []

    async def request(self, method, url, json=None, **kw):
        path = url.split("builderapps.osmike.com", 1)[-1] or "/"
        if method == "POST" and path == "/api/links":
            if self.mode == "write_fails":
                return FakeResponse(422, '{"error":"invalid input syntax for type integer"}')
            marker = (json or {}).get("title", "")
            self.rows.append(marker)
            return FakeResponse(201, '{"id":7,"title":"%s"}' % marker)
        if method == "GET" and path == "/api/links":
            if self.mode == "read_missing":
                return FakeResponse(200, '{"links":[]}')
            return FakeResponse(200, '{"links":%s}' % json_dumps(self.rows))
        return FakeResponse(404, "not found")


def json_dumps(rows):
    return "[" + ",".join('{"id":%d,"title":"%s"}' % (i, r) for i, r in enumerate(rows)) + "]"


FLOW = [{"name": "shorten a URL and see it listed", "method": "POST", "path": "/api/links",
         "body": {"url": "https://example.com/__MARKER__", "title": "__MARKER__"},
         "list": "/api/links", "page": "/"}]


async def _run_with(mode: str, page_text: str) -> dict:
    app = FakeApp(mode)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, json=None, **kw):
            return await app.request(method, url, json=json)

    import server.harness.semantic_qa as sq
    real_client, real_eval = sq.httpx.AsyncClient, chrome.eval_js
    sq.httpx.AsyncClient = lambda *a, **k: FakeClient()
    chrome.eval_js = lambda url, expr, **k: _async(page_text(app) if callable(page_text)
                                                   else page_text)
    try:
        return await sq.run_flows(PROJECT, "https://sem001.builderapps.osmike.com", FLOW)
    finally:
        sq.httpx.AsyncClient = real_client
        chrome.eval_js = real_eval


async def _async(v):
    return v


async def main() -> int:
    os.makedirs(WS, exist_ok=True)

    # --- 1. the real bug: rows exist, API returns them, the page says "No links yet" ---
    print("\n[the bug console-only QA passed]")
    out = await _run_with("ok", "My Links\nNo links yet")
    check("a rendered-but-missing record is a FINDING", len(out["findings"]) == 1, out)
    check("nothing counted as passed", out["passed"] == 0, out)
    f = out["findings"][0] if out["findings"] else ""
    check("the finding blames the FRONTEND",
          "FRONTEND is not rendering data the API returns" in f, f)
    check("the finding proves the API does have the row", "DOES return it" in f, f)
    check("the finding quotes what the page actually showed", "No links yet" in f, f)
    check("verdict is not_rendered", out["detail"][0]["verdict"] == "not_rendered", out)

    # --- 2. the write path is broken (the silent 422 class) ------------------
    print("\n[write path broken]")
    out = await _run_with("write_fails", "My Links")
    check("a rejected write is a finding", len(out["findings"]) == 1, out)
    check("it names the status", "HTTP 422" in out["findings"][0], out["findings"])
    check("verdict is write_failed", out["detail"][0]["verdict"] == "write_failed", out)

    # --- 3. the read-back is broken -----------------------------------------
    print("\n[read path broken]")
    out = await _run_with("read_missing", "My Links")
    check("a dropped row is a finding", len(out["findings"]) == 1, out)
    check("it says the write was accepted but not returned",
          "does NOT return" in out["findings"][0].replace("\n", " ")
          or "DOES not" in out["findings"][0], out["findings"])
    check("verdict is read_missing", out["detail"][0]["verdict"] == "read_missing", out)

    # --- 4. the working app passes ------------------------------------------
    print("\n[a working app passes]")
    out = await _run_with("ok", lambda app: "My Links\n" + "\n".join(app.rows))
    check("a rendered record passes", out["passed"] == 1 and not out["findings"], out)
    check("verdict is ok", out["detail"][0]["verdict"] == "ok", out)

    # --- 5. a blank page is never silently a pass ---------------------------
    print("\n[blank page]")
    out = await _run_with("ok", "")
    check("an unreadable page is a finding", len(out["findings"]) == 1, out)
    check("verdict is page_unreadable", out["detail"][0]["verdict"] == "page_unreadable", out)

    # --- 6. the planner refuses junk ----------------------------------------
    print("\n[planner guards]")
    introspect.routes = lambda pid: [{"method": "POST", "path": "/api/links"},
                                     {"method": "GET", "path": "/api/links"}]
    plans = {
        "invented route": '{"flows":[{"name":"x","create":{"method":"POST",'
                          '"path":"/api/invented","body":{"title":"__MARKER__"}},'
                          '"list":"/api/links","page":"/"}]}',
        "no marker": '{"flows":[{"name":"x","create":{"method":"POST","path":"/api/links",'
                     '"body":{"title":"hello"}},"list":"/api/links","page":"/"}]}',
        "not json": "I would test the link creation flow.",
    }
    for label, reply in plans.items():
        gpu.chat = (lambda r: (lambda *a, **k: _async(r)))(reply)
        got = await semantic_qa.plan_flows(PROJECT, "a link shortener", "")
        check(f"planner drops: {label}", got == [], got)

    gpu.chat = lambda *a, **k: _async(
        '{"flows":[{"name":"shorten","create":{"method":"POST","path":"/api/links",'
        '"body":{"url":"https://example.com/x","title":"__MARKER__"}},'
        '"list":"/api/links","page":"/"}]}')
    got = await semantic_qa.plan_flows(PROJECT, "a link shortener", "")
    check("planner accepts a valid flow", len(got) == 1, got)
    check("marker placeholder survives planning",
          "__MARKER__" in json.dumps(got[0]["body"]), got)

    # a read-only app has nothing to seed
    introspect.routes = lambda pid: [{"method": "GET", "path": "/api/links"}]
    check("read-only app plans no flows",
          await semantic_qa.plan_flows(PROJECT, "x", "") == [])

    # --- 7. markers are unique + injected everywhere -------------------------
    print("\n[markers]")
    m1, m2 = semantic_qa._marker(), semantic_qa._marker()
    check("markers are unique", m1 != m2)
    check("marker is a single DOM-safe token", m1.isalnum() and m1.startswith("QASEED"), m1)
    injected = semantic_qa._inject({"a": "__MARKER__", "b": ["x/__MARKER__"], "n": 3}, m1)
    check("marker injected in nested strings only",
          injected == {"a": m1, "b": [f"x/{m1}"], "n": 3}, injected)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        print("failed: " + ", ".join(_failed))
    return 1 if _failed else 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(rc)
