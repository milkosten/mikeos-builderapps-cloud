"""Syntax gate (phase 24) — validate the RESULT of every write/edit before it lands.

The single highest-value lesson from the SWE-agent paper: run a linter on every edit and
**reject the edit if the result isn't syntactically valid**. The model then never gets to
commit broken code — it gets the compiler's own error text back and corrects itself, which is
exactly the feedback loop a human has.

Validators, by extension:

* `.js` / `.cjs` / `.mjs` -> `node --check` (the real V8 parser; nothing else is honest).
* `.json`                 -> `json.loads` (with the line/column of the failure).
* `.sql`                  -> light sanity: balanced parens/quotes outside literals, no
  statement left dangling, plus the two house rules that have actually crash-looped a
  generated app (non-idempotent CREATE, a hand-rolled migration tracking table).
* `.html` / `.css`        -> cheap structural checks (unclosed html/body, brace balance).

**Where `node --check` runs (phase 24 step 3):** the control-plane image now carries the node
binary itself, copied from `node:20-bookworm-slim` in the Dockerfile (same major version as the
`node:20-alpine` runtime the generated apps use). That makes a check a single ~30 ms subprocess
instead of a container start per edit. If the binary is somehow missing (an older image), we
fall back to `docker run --rm -i node:20-alpine node --check` over stdin — correct but ~700 ms,
so it is a safety net, not the path. We deliberately do NOT exec into the project's own
`<id>-app` container: it may be down (that's often *why* we're editing) and the file being
validated isn't in it until the next rebuild.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

NODE_IMAGE = os.environ.get("BUILDERAPPS_NODE_IMAGE", "node:20-alpine")
_CHECK_TIMEOUT = 25.0

# Anything bigger than this is not source we should be parsing (RAM house rule).
MAX_VALIDATE_BYTES = 2 * 1024 * 1024

_JS_EXT = (".js", ".cjs", ".mjs")


def _node_bin() -> Optional[str]:
    return shutil.which("node")


async def _run(argv: list[str], stdin_data: Optional[bytes] = None) -> tuple[int, str]:
    """Run argv (never a shell string), bounded. Returns (rc, combined output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:  # noqa: BLE001 — binary missing / spawn refused
        return 127, str(e)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin_data), timeout=_CHECK_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return 124, "syntax check timed out"
    return proc.returncode, (out or b"")[:64 * 1024].decode("utf-8", "replace")


def _looks_esm(content: str) -> bool:
    return bool(re.search(r"^\s*(import\s.+\sfrom\s|export\s(default|const|function|class|\{))",
                          content, re.M))


async def check_js(content: str, ext: str = ".js") -> Optional[str]:
    """`node --check`. Returns None when valid, else the parser error text."""
    node = _node_bin()
    if node:
        suffix = ext if ext in _JS_EXT else ".js"
        if suffix == ".js" and _looks_esm(content):
            suffix = ".mjs"          # so node parses it as a module, not CJS
        tmpdir = tempfile.mkdtemp(prefix="syntaxchk-")
        path = os.path.join(tmpdir, "candidate" + suffix)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            rc, out = await _run([node, "--check", path])
            if rc == 0:
                return None
            # strip the temp path so the model sees a clean message
            return _clean_node_error(out.replace(path, "<file>"))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # Fallback: a throwaway node container reading the source on stdin.
    argv = ["docker", "run", "--rm", "-i", "--network", "none", NODE_IMAGE, "node"]
    if _looks_esm(content):
        argv += ["--input-type=module"]
    argv += ["--check"]
    rc, out = await _run(argv, content.encode("utf-8"))
    if rc == 0:
        return None
    if rc == 127:
        logger.warning("no node available for syntax check: %s", out[:200])
        return None          # never block a build because the checker is unavailable
    return _clean_node_error(out.replace("[stdin]", "<file>"))


def _clean_node_error(out: str) -> str:
    """Keep the useful head of a node syntax error (source line, caret, SyntaxError), drop the
    V8 internal stack — it is pure noise to the model and eats context."""
    lines = []
    for ln in out.splitlines():
        if re.match(r"\s+at ", ln):
            continue
        if ln.startswith("Node.js v"):
            continue
        lines.append(ln)
        if len(lines) >= 12:
            break
    return "\n".join(lines).strip() or "syntax error"


def check_json(content: str) -> Optional[str]:
    try:
        json.loads(content)
        return None
    except Exception as e:  # noqa: BLE001
        return f"invalid JSON: {e}"


def _strip_sql_noise(sql: str) -> str:
    """Blank out string literals, quoted identifiers and comments so structural checks see
    only SQL syntax (a paren inside 'a (b' is text, not structure)."""
    out = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i < n - 1 and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                if sql[i] == quote and (i + 1 >= n or sql[i + 1] != quote):
                    i += 1
                    break
                if sql[i] == quote:      # doubled '' / "" escape
                    i += 2
                    continue
                i += 1
            out.append(" ")
            continue
        if ch == "$" and re.match(r"\$[A-Za-z_]*\$", sql[i:i + 40] or ""):
            tag = re.match(r"\$[A-Za-z_]*\$", sql[i:i + 40]).group(0)
            end = sql.find(tag, i + len(tag))
            i = (end + len(tag)) if end != -1 else n
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def check_sql(content: str) -> Optional[str]:
    """Light sanity + the two house rules that have actually broken a generated app."""
    body = _strip_sql_noise(content)
    if not body.strip():
        return "the migration is empty (only comments/whitespace)"
    if body.count("(") != body.count(")"):
        return (f"unbalanced parentheses: {body.count('(')} '(' vs {body.count(')')} ')' "
                "— the statement looks incomplete")
    if content.count("'") % 2:
        return "unbalanced single quotes — a string literal is left open"
    tail = body.rstrip()
    if tail and not tail.endswith(";"):
        return ("the last statement does not end with ';' — SQL looks truncated mid-statement")
    # House rule: every DDL statement must be idempotent (migrations re-run on every boot).
    m = re.search(r"\bCREATE\s+(?:UNLOGGED\s+|TEMP\s+|TEMPORARY\s+)?TABLE\s+"
                  r"(?!IF\s+NOT\s+EXISTS)", body, re.I)
    if m:
        return ("CREATE TABLE without IF NOT EXISTS — migrations must be idempotent. "
                "Write `CREATE TABLE IF NOT EXISTS ...`.")
    m = re.search(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?!IF\s+NOT\s+EXISTS|CONCURRENTLY)",
                  body, re.I)
    if m:
        return ("CREATE INDEX without IF NOT EXISTS — migrations must be idempotent. "
                "Write `CREATE INDEX IF NOT EXISTS ...`.")
    # House rule 8: never a second migration tracking table.
    if re.search(r"\bschema_migrations\b", body, re.I):
        return ("do not create a `schema_migrations` table — db/migrate.js already tracks "
                "applied migrations in `_migrations`. Just add the numbered .sql file.")
    return None


def check_html(content: str) -> Optional[str]:
    low = content.lower()
    if "<html" in low and "</html>" not in low:
        return "unclosed <html> — the document is incomplete"
    if "<body" in low and "</body>" not in low:
        return "unclosed <body> — the document is incomplete"
    if low.count("<script") != low.count("</script>"):
        return (f"unbalanced <script> tags ({low.count('<script')} open, "
                f"{low.count('</script>')} closed)")
    return None


def check_css(content: str) -> Optional[str]:
    if content.count("{") != content.count("}"):
        return (f"unbalanced braces: {content.count('{')} '{{' vs {content.count('}')} '}}'")
    return None


async def validate(rel_path: str, content: str) -> Optional[str]:
    """Validate `content` as the future contents of `rel_path`.

    Returns None when it is acceptable, else a short, actionable error string. Unknown file
    types are accepted (we never block on a type we cannot honestly parse).
    """
    if len(content.encode("utf-8", "replace")) > MAX_VALIDATE_BYTES:
        return (f"file is larger than {MAX_VALIDATE_BYTES} bytes — split it up; the harness "
                "refuses to write source files that big")
    ext = os.path.splitext(rel_path)[1].lower()
    if ext in _JS_EXT:
        return await check_js(content, ext)
    if ext == ".json":
        return check_json(content)
    if ext == ".sql":
        return check_sql(content)
    if ext in (".html", ".htm"):
        return check_html(content)
    if ext == ".css":
        return check_css(content)
    return None
