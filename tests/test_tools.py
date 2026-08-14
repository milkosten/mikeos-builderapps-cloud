"""Self-contained tests for the agent tool layer (phases 23/24).

No pytest dependency (the control-plane image ships only the runtime deps) — run it directly:

    python3 -m tests.test_tools          # from the repo root, or inside the container

It builds a throwaway workspace under a temp dir, points WORKSPACES_ROOT at it, and asserts the
guarantees that matter: traversal refused, outputs capped, atomic writes, actionable edit
failures, and the syntax gate rejecting a broken result while leaving the file on disk intact.
"""
import asyncio
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tooltest-")
os.environ["WORKSPACES_ROOT"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.harness import syntax, tools  # noqa: E402

PROJECT = "tst001"
WS = os.path.join(_TMP, PROJECT)

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

app.get('/health', async (req, res) => {
  res.json({ status: 'ok', db: 'ok', redis: 'ok' });
});

app.get('/api/notes', async (req, res) => {
  const { rows } = await pool.query('SELECT * FROM notes ORDER BY created_at DESC');
  res.json({ notes: rows });
});

app.listen(3000);
"""


def setup() -> None:
    os.makedirs(os.path.join(WS, "public"), exist_ok=True)
    os.makedirs(os.path.join(WS, "migrations"), exist_ok=True)
    os.makedirs(os.path.join(WS, "db"), exist_ok=True)
    os.makedirs(os.path.join(WS, ".git"), exist_ok=True)
    with open(os.path.join(WS, "server.js"), "w") as fh:
        fh.write(SERVER_JS)
    with open(os.path.join(WS, "package.json"), "w") as fh:
        fh.write('{"name":"app","version":"1.0.0"}\n')
    with open(os.path.join(WS, "db", "migrate.js"), "w") as fh:
        fh.write("// platform migration runner\nmodule.exports = async () => {};\n")
    with open(os.path.join(WS, "migrations", "001_init.sql"), "w") as fh:
        fh.write("CREATE TABLE IF NOT EXISTS app_meta (k text primary key);\n")
    with open(os.path.join(WS, ".env"), "w") as fh:
        fh.write("DB_PASSWORD=supersecret\n")
    with open(os.path.join(WS, ".git", "config"), "w") as fh:
        fh.write("[core]\n")
    # a big file to prove the read/grep caps
    with open(os.path.join(WS, "public", "big.js"), "w") as fh:
        for i in range(4000):
            fh.write(f"// needle line {i} lorem ipsum dolor sit amet consectetur\n")


async def main() -> int:
    setup()
    tb = tools.Toolbox(PROJECT)

    # ---- 1. path confinement --------------------------------------------
    print("\n[1] path confinement")
    for bad in ["../../etc/passwd", "/etc/passwd", "../tst002/server.js",
                "public/../../../../etc/shadow", ".env", ".git/config"]:
        out = await tb.call("read_file", {"path": bad})
        check(f"read_file refused {bad!r}", out.startswith("ERROR:"), out[:120])
    out = await tb.call("write_file", {"path": "../../tmp/pwned.js", "content": "x=1;\n"})
    check("write_file refused traversal", out.startswith("ERROR:"), out[:120])
    check("no file escaped the workspace", not os.path.exists("/tmp/pwned.js"))
    # symlink escape
    os.symlink("/etc", os.path.join(WS, "escape"))
    out = await tb.call("read_file", {"path": "escape/passwd"})
    check("symlink escape refused", out.startswith("ERROR:"), out[:120])

    # ---- 2. output caps --------------------------------------------------
    print("\n[2] output caps")
    out = await tb.call("read_file", {"path": "public/big.js"})
    check("read_file windows by default", out.count("\n") <= tools.READ_DEFAULT_LINES + 2,
          f"{out.count(chr(10))} lines")
    check("read_file output under cap", len(out) <= tools.MAX_TOOL_OUTPUT + 120, str(len(out)))
    out = await tb.call("read_file", {"path": "public/big.js", "offset": 3000, "limit": 5000})
    check("read_file honours offset", "  3000\t" in out, out[:80])
    check("read_file clamps limit", out.count("\n") <= tools.READ_MAX_LINES + 2)
    out = await tb.call("grep", {"pattern": "needle"})
    check("grep capped at GREP_MAX_MATCHES",
          out.count("\n") <= tools.GREP_MAX_MATCHES + 3, f"{out.count(chr(10))} lines")
    check("grep output under cap", len(out) <= tools.MAX_TOOL_OUTPUT + 120, str(len(out)))
    out = await tb.call("grep", {"pattern": "/health", "glob": "*.js"})
    check("grep finds the health route", "server.js:" in out, out[:120])
    out = await tb.call("grep", {"pattern": "DB_PASSWORD"})
    check("grep never reads .env", ".env" not in out and "no match" in out, out[:120])
    out = await tb.call("list_files", {})
    check("list_files sees the repo", "db/migrate.js" in out and "server.js" in out, out[:200])
    check("list_files hides .git/.env", ".env" not in out and ".git/" not in out, out[:200])
    out = await tb.call("grep", {"pattern": "((("})
    check("bad regex is an actionable error", out.startswith("ERROR: invalid regex"), out[:120])

    # ---- 3. edit_file semantics -----------------------------------------
    print("\n[3] edit_file semantics")
    before = open(os.path.join(WS, "server.js")).read()
    out = await tb.call("edit_file", {"path": "server.js",
                                      "old_string": "app.get('/api/note', async (req, res)",
                                      "new_string": "x"})
    check("failed match returns NO MATCH", out.startswith("ERROR: NO MATCH"), out[:120])
    check("failed match returns nearby context", "Closest lines actually in the file" in out,
          out[:400])
    check("failed match left the file untouched",
          open(os.path.join(WS, "server.js")).read() == before)
    out = await tb.call("edit_file", {"path": "server.js", "old_string": "res.json",
                                      "new_string": "res.send"})
    check("ambiguous match reports occurrences", out.startswith("ERROR: AMBIGUOUS"), out[:160])
    check("ambiguous match lists line numbers", "lines " in out, out[:200])
    check("ambiguous match left the file untouched",
          open(os.path.join(WS, "server.js")).read() == before)
    out = await tb.call("edit_file", {"path": "server.js", "old_string": "app.listen(3000);",
                                      "new_string": "app.listen(process.env.PORT || 3000);"})
    check("unique edit applies", out.startswith("edited server.js"), out[:120])
    check("edit really landed",
          "process.env.PORT" in open(os.path.join(WS, "server.js")).read())
    out = await tb.call("edit_file", {"path": "server.js", "old_string": "res.json",
                                      "new_string": "res.json", "replace_all": True})
    check("no-op edit refused", out.startswith("ERROR:"), out[:120])
    out = await tb.call("edit_file", {"path": "nope.js", "old_string": "a", "new_string": "b"})
    check("edit of a missing file is actionable", "does not exist" in out, out[:120])

    # ---- 4. syntax gate (phase 24) --------------------------------------
    print("\n[4] syntax gate")
    have_node = syntax._node_bin() is not None
    before = open(os.path.join(WS, "server.js")).read()
    out = await tb.call("edit_file", {"path": "server.js",
                                      "old_string": "app.listen(process.env.PORT || 3000);",
                                      "new_string": "app.listen(process.env.PORT || 3000;"})
    if have_node:
        check("broken JS edit REJECTED", out.startswith("ERROR: REJECTED"), out[:200])
        check("rejection carries the parser error", "SyntaxError" in out, out[:300])
        check("file on disk unchanged after rejection",
              open(os.path.join(WS, "server.js")).read() == before)
    else:
        print("  skip (no node binary available)")
    out = await tb.call("write_file", {"path": "public/broken.js",
                                       "content": "function f() { return 1;\n"})
    if have_node:
        check("broken JS write REJECTED", out.startswith("ERROR: REJECTED"), out[:200])
        check("rejected write created no file",
              not os.path.exists(os.path.join(WS, "public", "broken.js")))
    out = await tb.call("write_file", {"path": "package.json", "content": '{"name": "app",}\n'})
    check("invalid JSON REJECTED", out.startswith("ERROR: REJECTED"), out[:200])
    check("package.json unchanged",
          open(os.path.join(WS, "package.json")).read() == '{"name":"app","version":"1.0.0"}\n')
    out = await tb.call("write_file", {
        "path": "migrations/002_notes.sql",
        "content": "CREATE TABLE notes (id bigserial primary key);\n"})
    check("non-idempotent CREATE TABLE REJECTED", out.startswith("ERROR: REJECTED"), out[:200])
    out = await tb.call("write_file", {
        "path": "migrations/002_notes.sql",
        "content": "CREATE TABLE IF NOT EXISTS schema_migrations (f text);\n"})
    check("hand-rolled migration table REJECTED", out.startswith("ERROR: REJECTED"), out[:200])
    out = await tb.call("write_file", {
        "path": "migrations/002_notes.sql",
        "content": "CREATE TABLE IF NOT EXISTS notes (\n  id bigserial primary key,\n"})
    check("truncated SQL REJECTED", out.startswith("ERROR: REJECTED"), out[:200])
    out = await tb.call("write_file", {
        "path": "migrations/002_notes.sql",
        "content": ("CREATE TABLE IF NOT EXISTS notes (\n  id bigserial primary key,\n"
                    "  body text not null,\n  created_at timestamptz not null default now()\n);\n"
                    "CREATE INDEX IF NOT EXISTS notes_created_idx ON notes (created_at);\n")})
    check("valid migration accepted", out.startswith("created migrations/002_notes.sql"), out[:200])
    out = await tb.call("check_syntax", {"path": "migrations/002_notes.sql"})
    check("check_syntax reports OK", out.endswith("OK"), out[:120])

    # ---- 5. policy rules -------------------------------------------------
    print("\n[5] policy rules")
    out = await tb.call("write_file", {"path": "migrations/notes.sql",
                                       "content": "CREATE TABLE IF NOT EXISTS x (a int);\n"})
    check("unnumbered migration refused", out.startswith("ERROR: refused"), out[:200])
    check("refusal names the next number", "003" in out, out[:200])
    out = await tb.call("write_file", {"path": "db/migrate.js", "content": "// hijack\n"})
    check("db/migrate.js protected", out.startswith("ERROR: refused"), out[:200])
    check("db/migrate.js untouched",
          "platform migration runner" in open(os.path.join(WS, "db", "migrate.js")).read())

    # ---- 6. atomic writes ------------------------------------------------
    print("\n[6] atomic writes")
    out = await tb.call("write_file", {"path": "public/app.js",
                                       "content": "const ok = true;\nconsole.log(ok);\n"})
    check("write_file creates", out.startswith("created public/app.js"), out[:120])
    leftovers = [f for f in os.listdir(os.path.join(WS, "public")) if f.startswith(".tmp-")]
    check("no temp files left behind", not leftovers, str(leftovers))
    check("changed-file set is tracked",
          tb.changed.get("public/app.js") == "created"
          and tb.changed.get("server.js") == "modified", str(tb.changed))

    # ---- 7. misc ---------------------------------------------------------
    print("\n[7] misc")
    out = await tb.call("bogus_tool", {})
    check("unknown tool is an error", out.startswith("ERROR: unknown tool"), out[:120])
    out = await tb.call("read_file", "{not json")
    check("malformed arguments are repaired/reported", out.startswith("ERROR:"), out[:120])
    out = await tb.call("finish", {"summary": "did the thing"})
    check("finish records the summary", tb.finished and tb.summary == "did the thing", out)

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
