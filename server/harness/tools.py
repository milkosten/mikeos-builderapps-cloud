"""The tool layer (phase 23) — the codegen agent's hands.

Instead of asking the model to return whole files as one JSON blob (every failure this
platform has hit traces back to that: a COMPLETE file misjudged as truncated killing a whole
build, a silently-rewritten `/health` contract, a re-invented migration runner, a frontend
calling an endpoint it could not read), the model gets real tools and *navigates* the repo:

    read_file · list_files · grep · write_file · edit_file · check_syntax · app_logs · finish

Design rules, straight from the research in HARNESS-TOOLS.md:

* **Everything is confined to the project workspace** via `introspect.safe_path` — `..`,
  absolute paths and symlink escapes all raise, and `.git`/`node_modules`/`.env*` are not
  browsable. A tool can never touch the host or another tenant.
* **Every output is capped** — bytes per read, matches per grep, entries per listing, log
  lines. A file is never slurped whole into RAM (the 1.55 GB house rule).
* **Writes are atomic** (tmp + `os.replace`) so a crash can never leave a half-file, and they
  are **syntax-gated** (see `server.harness.syntax`): a broken result is rejected with the
  parser's error and the file on disk is left untouched.
* **Errors are the product.** Aider's benchmarks: a search/replace block matches only ~70-80%
  of the time against an evolved file, so a failed match must come back as an actionable
  message *with nearby context*, never as a silent no-op.
* **No raw shell.** `app_logs` is a fixed argv against `<id>-app`; there is no escape hatch.
"""
import difflib
import fnmatch
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from server import introspect, workspace
from server.harness import syntax

logger = logging.getLogger(__name__)

# ---- hard bounds ----------------------------------------------------------
MAX_TOOL_OUTPUT = 12000        # chars returned to the model from ANY tool
READ_DEFAULT_LINES = 200
READ_MAX_LINES = 600
READ_MAX_BYTES = 512 * 1024    # per read; larger files are windowed, never slurped
GREP_MAX_MATCHES = 60
GREP_MAX_FILE_BYTES = 1024 * 1024
LIST_MAX_ENTRIES = 300
LOG_MAX_LINES = 200
WRITE_MAX_BYTES = 512 * 1024   # a single generated source file over this is a bug

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".cache", "dist", "build", ".next"}
_BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".gz",
               ".woff", ".woff2", ".ttf", ".mp4", ".mp3", ".wasm"}

_MIGRATION_RE = re.compile(r"^migrations/(\d{3})_[a-z0-9_]+\.sql$")


class ToolError(Exception):
    """A tool refused to act. The message is fed back to the model verbatim."""


def _cap(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [output truncated at {limit} chars — narrow your query]"


# ---------------------------------------------------------------------------
# JSON schemas handed to the model (OpenAI-style function calling)
# ---------------------------------------------------------------------------
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": ("Read a window of a file in the project, with line numbers. "
                            "Use this before editing anything — never guess a file's "
                            "contents. Defaults to the first 200 lines; page through a big "
                            "file with `offset`."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Path relative to the project root, e.g. server.js"},
                    "offset": {"type": "integer",
                               "description": "1-based first line to show (default 1)"},
                    "limit": {"type": "integer",
                              "description": f"How many lines (default {READ_DEFAULT_LINES}, "
                                             f"max {READ_MAX_LINES})"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": ("List files in the project (recursive, capped). Use it to see what "
                            "already exists before creating anything — e.g. db/migrate.js and "
                            "the existing migrations/NNN_*.sql files."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Subdirectory to list; omit for the whole project"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": ("Search the project's source for a regular expression. Returns "
                            "path:line: matched-line, capped. Use it to find the route, "
                            "handler or DOM id you need before you edit."),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python/PCRE-style regex"},
                    "glob": {"type": "string",
                             "description": "Optional filename filter, e.g. *.js or public/*"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": ("Create a NEW file (or fully replace a small one) with complete "
                            "contents. The result is syntax-checked before it lands; if it is "
                            "invalid nothing is written and you get the parser error back. For "
                            "an existing file prefer edit_file — do not re-emit a large file."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the project root"},
                    "content": {"type": "string", "description": "The file's full contents"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": ("THE PRIMARY EDITOR. Replace an exact string in a file. "
                            "`old_string` must appear EXACTLY once (include surrounding lines "
                            "to make it unique) unless you set replace_all. The result is "
                            "syntax-checked before it lands. Use this for every change to an "
                            "existing file."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the project root"},
                    "old_string": {"type": "string",
                                   "description": "Exact text to find (copy it from read_file "
                                                  "output, without the line numbers)"},
                    "new_string": {"type": "string",
                                   "description": "Replacement text (empty string deletes)"},
                    "replace_all": {"type": "boolean",
                                    "description": "Replace every occurrence (default false)"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_syntax",
            "description": ("Validate a file already on disk (node --check for .js, JSON.parse "
                            "for .json, sanity checks for .sql/.html/.css)."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the project root"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "app_logs",
            "description": ("Tail the running app container's stdout/stderr. Use it to see why "
                            "the app crashed or which request 500'd."),
            "parameters": {
                "type": "object",
                "properties": {
                    "tail": {"type": "integer",
                             "description": f"How many lines (default 80, max {LOG_MAX_LINES})"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": ("Call this when the feature is fully implemented (or when you have "
                            "decided nothing needs to change). Summarise what you changed and "
                            "why in one or two sentences."),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "What you changed and why"},
                },
                "required": ["summary"],
            },
        },
    },
]

TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]


# ---------------------------------------------------------------------------
class Toolbox:
    """One agent session's tools, bound to a single project workspace."""

    def __init__(self, project_id: str, *, on_event: Optional[Callable[[dict], None]] = None):
        self.project_id = introspect.assert_shortid(project_id)
        self.root = workspace.path_for(self.project_id)
        self.changed: dict[str, str] = {}      # relpath -> "created" | "modified"
        self.finished = False
        self.summary = ""
        self.calls = 0
        self._on_event = on_event

    # -- path plumbing ------------------------------------------------------
    def _resolve(self, path: Any) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ToolError("`path` is required and must be a string")
        try:
            return introspect.safe_path(self.project_id, path.strip())
        except introspect.BadPath as e:
            raise ToolError(
                f"refused: {e}. Paths must be relative to the project root and stay inside it "
                f"(no '..', no absolute paths, no .git/.env/node_modules).") from None

    def _rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.root.resolve()))
        except Exception:  # noqa: BLE001
            return p.name

    # -- read ---------------------------------------------------------------
    def read_file(self, path: str, offset: int = 1, limit: int = READ_DEFAULT_LINES) -> str:
        p = self._resolve(path)
        rel = self._rel(p)
        if p.is_dir():
            raise ToolError(f"{rel} is a directory — use list_files")
        if not p.is_file():
            raise ToolError(f"{rel} does not exist. Use list_files to see what is there.")
        try:
            offset = max(1, int(offset or 1))
        except (TypeError, ValueError):
            offset = 1
        try:
            limit = max(1, min(int(limit or READ_DEFAULT_LINES), READ_MAX_LINES))
        except (TypeError, ValueError):
            limit = READ_DEFAULT_LINES
        if p.suffix.lower() in _BINARY_EXT:
            return f"{rel}: binary file ({p.stat().st_size} bytes), not shown"
        text, truncated, is_bin = introspect._read_capped(p, READ_MAX_BYTES)
        if is_bin:
            return f"{rel}: binary file, not shown"
        lines = text.splitlines()
        total = len(lines)
        window = lines[offset - 1: offset - 1 + limit]
        if not window:
            return (f"{rel} has {total} lines; offset {offset} is past the end.")
        body = "\n".join(f"{offset + i:6d}\t{ln}" for i, ln in enumerate(window))
        head = (f"{rel} — lines {offset}-{offset + len(window) - 1} of {total}"
                + (" (file truncated at read cap)" if truncated else ""))
        return _cap(f"{head}\n{body}")

    # -- list ---------------------------------------------------------------
    def list_files(self, path: str = "") -> str:
        # An empty path is the natural "show me the project" call, and it cannot escape
        # anything — resolve it to the workspace root instead of rejecting it.
        rel_in = (path or "").strip().strip("/")
        p = self._resolve(rel_in) if rel_in else self.root.resolve()
        rel_root = self._rel(p) if rel_in else ""
        if p.is_file():
            return f"{self._rel(p)} ({p.stat().st_size} bytes)"
        if not p.is_dir():
            raise ToolError(f"{path or '.'} does not exist")
        rows: list[str] = []
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in _SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith(".env"):
                    continue
                fp = Path(dirpath) / name
                try:
                    size = fp.stat().st_size
                except OSError:
                    continue
                rows.append(f"{self._rel(fp)}\t{size}")
                if len(rows) >= LIST_MAX_ENTRIES:
                    break
            if len(rows) >= LIST_MAX_ENTRIES:
                rows.append(f"... [listing capped at {LIST_MAX_ENTRIES} entries]")
                break
        head = f"{len(rows)} file(s) under {rel_root or 'the project root'} (path\tbytes):"
        return _cap(head + "\n" + "\n".join(rows))

    # -- grep ---------------------------------------------------------------
    def grep(self, pattern: str, glob: Optional[str] = None) -> str:
        if not isinstance(pattern, str) or not pattern:
            raise ToolError("`pattern` is required")
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"invalid regex: {e}") from None
        hits: list[str] = []
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in _SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith(".env") or Path(name).suffix.lower() in _BINARY_EXT:
                    continue
                fp = Path(dirpath) / name
                rel = self._rel(fp)
                if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(name, glob)):
                    continue
                try:
                    if fp.stat().st_size > GREP_MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                scanned += 1
                text, _, is_bin = introspect._read_capped(fp, GREP_MAX_FILE_BYTES)
                if is_bin:
                    continue
                for i, ln in enumerate(text.splitlines(), 1):
                    if rx.search(ln):
                        hits.append(f"{rel}:{i}: {ln.strip()[:200]}")
                        if len(hits) >= GREP_MAX_MATCHES:
                            break
                if len(hits) >= GREP_MAX_MATCHES:
                    break
            if len(hits) >= GREP_MAX_MATCHES:
                break
        if not hits:
            return (f"no match for /{pattern}/" + (f" in {glob}" if glob else "")
                    + f" ({scanned} files searched)")
        note = (f"\n... [capped at {GREP_MAX_MATCHES} matches — narrow the pattern]"
                if len(hits) >= GREP_MAX_MATCHES else "")
        return _cap(f"{len(hits)} match(es):\n" + "\n".join(hits) + note)

    # -- write --------------------------------------------------------------
    def _policy_check(self, rel: str, exists: bool) -> None:
        """Deterministic house rules that a prompt alone has failed to enforce."""
        if rel.startswith("migrations/") and not exists:
            if not _MIGRATION_RE.match(rel):
                raise ToolError(
                    f"refused: '{rel}' is not a valid migration name. New migrations must be "
                    f"migrations/NNN_snake_case.sql — the next free number here is "
                    f"{self._next_migration_number():03d} (run list_files migrations/ to check).")
        if rel in ("db/migrate.js",) and exists:
            raise ToolError(
                "refused: db/migrate.js is the platform's migration runner and must not be "
                "modified. To add schema, create a new migrations/NNN_*.sql file instead.")

    def _next_migration_number(self) -> int:
        d = self.root / "migrations"
        nums = []
        if d.is_dir():
            for f in os.listdir(d):
                m = re.match(r"(\d+)_", f)
                if m:
                    nums.append(int(m.group(1)))
        return (max(nums) + 1) if nums else 1

    def _atomic_write(self, p: Path, content: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Every file here is copied into the project's image and read by its non-root `node`
        # user, so it MUST stay world-readable. mkstemp creates 0600, and a 0600 server.js
        # crash-loops the container on boot with EACCES (observed live — the agent had to work
        # around it by chmod'ing in the Dockerfile). Keep any extra bits the file already had
        # (e.g. +x on a script) but always force owner-write + everyone-read.
        try:
            mode = (p.stat().st_mode & 0o777) | 0o644
        except OSError:
            mode = 0o644
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-", suffix=p.suffix)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, p)          # atomic within the same filesystem
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    async def write_file(self, path: str, content: str) -> str:
        p = self._resolve(path)
        rel = self._rel(p)
        if not isinstance(content, str):
            raise ToolError("`content` must be a string")
        if len(content.encode("utf-8", "replace")) > WRITE_MAX_BYTES:
            raise ToolError(f"refused: {rel} would be over {WRITE_MAX_BYTES} bytes — "
                            "split the work into smaller files/edits")
        existed = p.is_file()
        self._policy_check(rel, existed)
        err = await syntax.validate(rel, content)
        if err:
            raise ToolError(
                f"REJECTED — {rel} was NOT written because the content is not valid:\n{err}\n"
                "Fix the problem and call write_file again. The file on disk is unchanged.")
        self._atomic_write(p, content)
        self.changed[rel] = "modified" if existed else "created"
        return (f"{'updated' if existed else 'created'} {rel} "
                f"({len(content.splitlines())} lines, syntax OK)")

    # -- edit ---------------------------------------------------------------
    def _near_miss(self, text: str, old: str) -> str:
        """Actionable failure context: the closest lines we DID find (Aider's lesson — the
        failure message is the product)."""
        needle = next((ln.strip() for ln in old.splitlines() if ln.strip()), "")
        if not needle:
            return ""
        lines = text.splitlines()
        scored = []
        for i, ln in enumerate(lines, 1):
            r = difflib.SequenceMatcher(None, needle, ln.strip()).ratio()
            if r > 0.5:
                scored.append((r, i, ln))
        scored.sort(reverse=True)
        if not scored:
            return ("\nNo similar line was found either — read_file the file again; it may have "
                    "changed, or you may be editing the wrong file.")
        out = ["\nClosest lines actually in the file (copy one of these EXACTLY):"]
        for r, i, ln in scored[:3]:
            out.append(f"{i:6d}\t{ln[:200]}")
        return "\n".join(out)

    async def edit_file(self, path: str, old_string: str, new_string: str,
                        replace_all: bool = False) -> str:
        p = self._resolve(path)
        rel = self._rel(p)
        if not p.is_file():
            raise ToolError(f"{rel} does not exist — use write_file to create it, or "
                            f"list_files to find the right path.")
        if not isinstance(old_string, str) or old_string == "":
            raise ToolError("`old_string` must be a non-empty string")
        if not isinstance(new_string, str):
            raise ToolError("`new_string` must be a string")
        if old_string == new_string:
            raise ToolError("old_string and new_string are identical — nothing to do")
        if p.stat().st_size > READ_MAX_BYTES:
            raise ToolError(f"{rel} is too large to edit safely ({p.stat().st_size} bytes)")
        text = p.read_text("utf-8", "replace")
        count = text.count(old_string)
        if count == 0:
            raise ToolError(
                f"NO MATCH — `old_string` does not appear in {rel}, so nothing was changed."
                + self._near_miss(text, old_string)
                + "\nRemember: match the file EXACTLY (whitespace included) and do not include "
                  "the line numbers from read_file.")
        if count > 1 and not replace_all:
            locs = []
            pos = 0
            while len(locs) < 5:
                pos = text.find(old_string, pos)
                if pos == -1:
                    break
                locs.append(text.count("\n", 0, pos) + 1)
                pos += len(old_string)
            raise ToolError(
                f"AMBIGUOUS — `old_string` appears {count} times in {rel} (lines "
                f"{', '.join(str(l) for l in locs)}), so nothing was changed. Include more "
                f"surrounding context to make it unique, or set replace_all=true.")
        updated = (text.replace(old_string, new_string) if replace_all
                   else text.replace(old_string, new_string, 1))
        err = await syntax.validate(rel, updated)
        if err:
            raise ToolError(
                f"REJECTED — the edit would leave {rel} syntactically invalid, so it was NOT "
                f"applied:\n{err}\nThe file on disk is unchanged. Read the surrounding code and "
                "try a corrected edit.")
        self._atomic_write(p, updated)
        self.changed[rel] = self.changed.get(rel, "modified")
        n = count if replace_all else 1
        return f"edited {rel} ({n} replacement{'s' if n > 1 else ''}, syntax OK)"

    # -- check --------------------------------------------------------------
    async def check_syntax(self, path: str) -> str:
        p = self._resolve(path)
        rel = self._rel(p)
        if not p.is_file():
            raise ToolError(f"{rel} does not exist")
        if p.stat().st_size > syntax.MAX_VALIDATE_BYTES:
            raise ToolError(f"{rel} is too large to validate")
        err = await syntax.validate(rel, p.read_text("utf-8", "replace"))
        return f"{rel}: OK" if err is None else f"{rel}: INVALID\n{err}"

    # -- logs ---------------------------------------------------------------
    async def app_logs(self, tail: int = 80) -> str:
        try:
            tail = max(1, min(int(tail or 80), LOG_MAX_LINES))
        except (TypeError, ValueError):
            tail = 80
        lines = await introspect.logs(self.project_id, tail)   # fixed argv, no shell
        if not lines:
            return ("no logs — the app container is not running yet (it starts after this "
                    "feature is deployed).")
        return _cap("\n".join(lines[-tail:]))

    # -- finish -------------------------------------------------------------
    def finish(self, summary: str = "") -> str:
        self.finished = True
        self.summary = (summary or "").strip()[:2000]
        return "done"

    # -- dispatch -----------------------------------------------------------
    async def call(self, name: str, arguments: Any) -> str:
        """Run one tool call. NEVER raises: a refusal is returned as text so the model can
        correct itself (a silent no-op teaches it nothing)."""
        self.calls += 1
        args = arguments
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except Exception:  # noqa: BLE001 — provider quirk: repair, don't crash
                s = args.strip()
                if s.startswith("```"):
                    s = s.strip("`")
                    s = s.split("\n", 1)[-1]
                try:
                    args = json.loads(s)
                except Exception:  # noqa: BLE001
                    return (f"ERROR: could not parse the arguments for {name} as JSON. "
                            f"Send valid JSON. (got: {str(arguments)[:200]})")
        if not isinstance(args, dict):
            return f"ERROR: arguments for {name} must be a JSON object"
        try:
            if name == "read_file":
                return self.read_file(args.get("path"), args.get("offset", 1),
                                      args.get("limit", READ_DEFAULT_LINES))
            if name == "list_files":
                return self.list_files(args.get("path", "") or "")
            if name == "grep":
                return self.grep(args.get("pattern"), args.get("glob"))
            if name == "write_file":
                return await self.write_file(args.get("path"), args.get("content"))
            if name == "edit_file":
                return await self.edit_file(args.get("path"), args.get("old_string"),
                                            args.get("new_string"),
                                            bool(args.get("replace_all", False)))
            if name == "check_syntax":
                return await self.check_syntax(args.get("path"))
            if name == "app_logs":
                return await self.app_logs(args.get("tail", 80))
            if name == "finish":
                return self.finish(args.get("summary", ""))
            return (f"ERROR: unknown tool '{name}'. Available: {', '.join(TOOL_NAMES)}")
        except ToolError as e:
            return f"ERROR: {e}"
        except Exception as e:  # noqa: BLE001 — never let a tool bug kill the run
            logger.exception("tool %s failed", name)
            return f"ERROR: {name} failed: {e}"
