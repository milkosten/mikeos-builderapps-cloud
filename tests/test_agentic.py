"""Offline tests for the agentic codegen loop (phases 26/27).

Drives `server.harness.agentic` with a SCRIPTED model (gpu.chat_tools monkeypatched) so the
loop itself — tool dispatch, the syntax gate rejecting a bad edit mid-flight, the call budget,
the forced finish, and the artifact transcript — is proven without spending a token.

    python3 -m tests.test_agentic
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="agentictest-")
os.environ["WORKSPACES_ROOT"] = os.path.join(_TMP, "workspaces")
os.environ["ARTIFACTS_ROOT"] = os.path.join(_TMP, "artifacts")
os.environ["BUILDERAPPS_AGENT_MAX_TOOLS"] = "6"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import gpu  # noqa: E402
from server.harness import agentic, artifacts  # noqa: E402

PROJECT = "tst002"
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


SERVER_JS = """\
const express = require('express');
const app = express();

app.get('/health', (req, res) => res.json({ status: 'ok', db: 'ok', redis: 'ok' }));

app.listen(process.env.PORT || 3000);
"""


def _call(name, args, i=0):
    return {"id": f"c{i}", "name": name, "arguments": json.dumps(args)}


def script(replies):
    """Return a fake gpu.chat_tools that plays `replies` in order."""
    state = {"i": 0}

    async def fake(messages, tools, **kw):
        i = state["i"]
        state["i"] += 1
        if i >= len(replies):
            return {"content": "", "tool_calls": [_call("finish", {"summary": "out of script"})],
                    "finish_reason": "tool_calls", "raw": None, "usage": {}}
        return {"content": "", "tool_calls": replies[i], "finish_reason": "tool_calls",
                "raw": None, "usage": {"completion_tokens": 10}}
    return fake


def setup():
    os.makedirs(WS, exist_ok=True)
    with open(os.path.join(WS, "server.js"), "w") as fh:
        fh.write(SERVER_JS)


async def main() -> int:
    setup()
    real = gpu.chat_tools

    # ---- 1. read -> (rejected) edit -> good edit -> finish ----------------
    print("\n[1] full loop: look, edit, get rejected, correct, finish")
    gpu.chat_tools = script([
        [_call("list_files", {})],
        [_call("read_file", {"path": "server.js"})],
        # syntactically broken result — the gate must refuse it
        [_call("edit_file", {"path": "server.js",
                             "old_string": "app.listen(process.env.PORT || 3000);",
                             "new_string": "app.get('/api/notes', (req, res) => { res.json({notes: []})\n"
                                           "app.listen(process.env.PORT || 3000);"})],
        [_call("edit_file", {"path": "server.js",
                             "old_string": "app.listen(process.env.PORT || 3000);",
                             "new_string": "app.get('/api/notes', (req, res) => res.json({ notes: [] }));\n\n"
                                           "app.listen(process.env.PORT || 3000);"})],
        [_call("finish", {"summary": "added GET /api/notes"})],
    ])
    res = await agentic.run_agent(project_id=PROJECT, run_id=41, step="build_01",
                                  brief="a notes app", tech_plan="## Routes\n- GET /api/notes",
                                  task="add GET /api/notes")
    check("only the file it edited is reported changed", res["changed"] == ["server.js"],
          str(res["changed"]))
    check("summary comes from finish()", res["summary"] == "added GET /api/notes", res["summary"])
    check("loop ran 5 tool calls", res["tool_calls"] == 5, str(res["tool_calls"]))
    body = open(os.path.join(WS, "server.js")).read()
    check("the good edit landed", "res.json({ notes: [] })" in body)
    check("the rejected edit never landed", body.count("app.listen") == 1, body)
    check("file is still valid JS", "req, res) => { res.json({notes: []})" not in body)

    # ---- 2. artifacts (phase 27) ----------------------------------------
    print("\n[2] artifacts")
    path = res["artifact"]
    check("artifact file exists", os.path.isfile(path), path)
    lines = [json.loads(l) for l in open(path)]
    kinds = [l["kind"] for l in lines]
    check("seed persisted first", kinds[0] == "seed", str(kinds[:3]))
    check("every raw response persisted", kinds.count("llm_response") == 5, str(kinds))
    check("every tool call + result persisted", kinds.count("tool_call") == 5, str(kinds))
    check("response is recorded BEFORE the tool it requested",
          kinds.index("llm_response") < kinds.index("tool_call"), str(kinds))
    check("the rejection is in the transcript",
          any("REJECTED" in str(l.get("result", "")) for l in lines))
    check("end record carries the changed set",
          lines[-1]["kind"] == "end" and lines[-1]["changed"] == ["server.js"], str(lines[-1]))
    check("artifact path is <project>/<run>/<step>-<attempt>.jsonl",
          path.endswith(f"{PROJECT}/41/build_01-0.jsonl"), path)

    # ---- 3. budget + forced finish --------------------------------------
    print("\n[3] call budget")
    gpu.chat_tools = script([[_call("read_file", {"path": "server.js"})] for _ in range(20)])
    res2 = await agentic.run_agent(project_id=PROJECT, run_id=42, step="build_02",
                                   brief="b", tech_plan="", task="thrash",
                                   require_change=False)
    check("budget capped the loop", res2["tool_calls"] <= agentic.MAX_TOOL_CALLS + 1,
          str(res2["tool_calls"]))
    check("nothing was changed by a read-only thrash", res2["changed"] == [], str(res2["changed"]))

    # ---- 4. retention ----------------------------------------------------
    print("\n[4] retention")
    base = os.path.join(os.environ["ARTIFACTS_ROOT"], PROJECT)
    for r in range(100, 120):
        os.makedirs(os.path.join(base, str(r)), exist_ok=True)
        open(os.path.join(base, str(r), "x-0.jsonl"), "w").close()
    artifacts.prune(PROJECT, keep=5)
    check("prune keeps only the newest N runs",
          len([d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]) == 5,
          str(sorted(os.listdir(base))))

    gpu.chat_tools = real
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
