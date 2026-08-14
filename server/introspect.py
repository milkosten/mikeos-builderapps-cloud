"""Project introspection — the read-only data behind the builder UI's tabs.

Everything here runs against UNTRUSTED, AI-generated projects, so it is written defensively:

  * `assert_shortid()` gates every project id against ^[0-9a-z]{6}$ BEFORE it can reach a
    docker/psql/redis argv. Docker is always invoked with an argument LIST (never a shell
    string), so there is no command-injection surface.
  * `safe_path()` resolves any client-supplied path inside the workspace and rejects
    anything that escapes it (`..`, absolute paths, symlinks out) -> ValueError -> HTTP 400.
  * File reads are CAPPED (512 KB) and read incrementally — a project file is never slurped
    whole into RAM (the 1.55 GB house rule). Binaries are reported, never streamed.
  * DB introspection is READ-ONLY: a fixed information_schema/pg_catalog query with the
    identifiers quoted server-side by `format(%I)`; no row DATA is exposed, only schema and
    counts.
  * Every list is bounded (log tail, redis keys, files scanned, table count) so one HTTP call
    can never exhaust the control plane's memory.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import ssl
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SITES_BASE = os.environ.get("SITES_BASE", "builderapps.osmike.com")
GITEA_URL = os.environ.get("GITEA_URL", "https://gitea.osmike.com").rstrip("/")

# ---- hard bounds (one call must never be able to exhaust memory) ----------
FILE_READ_CAP = 512 * 1024          # 512 KB per /file read, then truncated:true
SOURCE_SCAN_CAP = 256 * 1024        # per-file cap when scanning source for routes
MAX_ENTRIES = 500                   # dir listing
MAX_LOG_LINES = 2000
MAX_LOG_BYTES = 1024 * 1024
MAX_REDIS_KEYS = 50
MAX_TABLES = 200
MAX_COMMITS = 100
MAX_SCAN_FILES = 60

_SHORTID_RE = re.compile(r"^[0-9a-z]{6}$")
_DOC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(md|markdown)$")

# Never surfaced through the file browser: .git internals are noise, and the rendered .env
# holds DB_PASSWORD/APP_SECRET in cleartext (those have their own, masked /secrets endpoint).
_HIDDEN_NAMES = {".git", "node_modules", "__pycache__"}
_HIDDEN_PREFIXES = (".env",)

# An env var whose KEY looks like a credential is masked outright.
_SECRETISH_KEY_RE = re.compile(r"(password|passwd|secret|token|key|credential)", re.I)
# ...and credentials embedded in a URL value (postgres://user:pw@host) are redacted too.
_URL_CRED_RE = re.compile(r"://([^:/@\s]+):([^@/\s]+)@")


class BadPath(ValueError):
    """Client-supplied path escaped the workspace (or is otherwise not allowed)."""


def assert_shortid(project_id: str) -> str:
    """Gate a project id before it can reach any subprocess argv. Raises BadPath."""
    if not isinstance(project_id, str) or not _SHORTID_RE.match(project_id):
        raise BadPath("invalid project id")
    return project_id


# --------------------------------------------------------------------------
# subprocess plumbing — ALWAYS an argv list, never a shell string
# --------------------------------------------------------------------------
async def run_argv(argv: list[str], *, timeout: float = 30.0,
                   max_bytes: int = MAX_LOG_BYTES) -> tuple[int, str]:
    """Run argv, capture combined output (bounded). Returns (rc, output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:  # noqa: BLE001
        logger.info("spawn failed for %s: %s", argv[:2], e)
        return 127, str(e)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return 124, "timed out"
    return proc.returncode, (out or b"")[:max_bytes].decode("utf-8", "replace")


async def _container_state(name: str) -> Optional[str]:
    rc, out = await run_argv(
        ["docker", "inspect", "-f", "{{.State.Status}}", name], timeout=20)
    return out.strip() if rc == 0 and out.strip() else None


# --------------------------------------------------------------------------
# workspace path safety
# --------------------------------------------------------------------------
def workspace_root(project_id: str) -> Path:
    from server import workspace as ws
    return ws.path_for(assert_shortid(project_id))


def safe_path(project_id: str, relpath: str) -> Path:
    """Resolve `relpath` INSIDE the project's workspace, or raise BadPath.

    Rejects absolute paths, `..` traversal and symlinks that point out of the tree
    (Path.resolve() follows links, so is_relative_to catches the symlink case too)."""
    root = workspace_root(project_id).resolve()
    rel = (relpath or "").strip()
    if "\x00" in rel:
        raise BadPath("invalid path")
    # A bare "/" is the natural UI idiom for "the repo root" and cannot escape anything, so
    # it is accepted as the empty path. Every OTHER absolute path is rejected outright rather
    # than quietly re-rooted — silently turning "/etc/passwd" into "<workspace>/etc/passwd"
    # would answer 404 and hide the attempt.
    if rel == "/":
        rel = ""
    elif rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", rel):
        raise BadPath("absolute paths are not allowed")
    candidate = (root / rel).resolve()
    if candidate != root and not candidate.is_relative_to(root):
        raise BadPath("path escapes the project workspace")
    # also refuse to walk into hidden/secret areas
    for part in candidate.relative_to(root).parts:
        if part in _HIDDEN_NAMES or part.startswith(_HIDDEN_PREFIXES):
            raise BadPath("path is not browsable")
    return candidate


def _read_capped(path: Path, cap: int = FILE_READ_CAP) -> tuple[str, bool, bool]:
    """Read at most `cap` bytes. Returns (text, truncated, is_binary). Never loads the whole
    file: we read cap+1 bytes and stop."""
    with path.open("rb") as fh:
        chunk = fh.read(cap + 1)
    truncated = len(chunk) > cap
    chunk = chunk[:cap]
    if b"\x00" in chunk[:8192]:
        return "", False, True
    return chunk.decode("utf-8", "replace"), truncated, False


# --------------------------------------------------------------------------
# 1 + 2 — docs
# --------------------------------------------------------------------------
def _doc_title(path: Path) -> str:
    """First markdown H1, else a title-cased filename. Reads only the first 4 KB."""
    try:
        with path.open("rb") as fh:
            head = fh.read(4096).decode("utf-8", "replace")
        for line in head.splitlines():
            if line.startswith("# "):
                return line[2:].strip()[:120]
    except Exception:  # noqa: BLE001
        pass
    stem = path.stem.replace("-", " ").replace("_", " ").strip()
    return stem.title()[:120]


def list_docs(project_id: str) -> list[dict]:
    d = workspace_root(project_id) / "docs"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if not p.is_file() or not _DOC_NAME_RE.match(p.name):
            continue
        try:
            out.append({"name": p.name, "title": _doc_title(p), "size": p.stat().st_size})
        except OSError:
            continue
    return out


def read_doc(project_id: str, name: str) -> Optional[dict]:
    if not _DOC_NAME_RE.match(name or ""):
        raise BadPath("invalid document name")
    p = safe_path(project_id, f"docs/{name}")
    if not p.is_file():
        return None
    text, truncated, is_binary = _read_capped(p)
    if is_binary:
        raise BadPath("not a markdown document")
    return {"name": name, "markdown": text, "truncated": truncated}


# --------------------------------------------------------------------------
# 3 + 4 — file tree / file content
# --------------------------------------------------------------------------
def list_files(project_id: str, relpath: str = "") -> list[dict]:
    root = workspace_root(project_id).resolve()
    target = safe_path(project_id, relpath)
    if not target.is_dir():
        return []
    entries: list[dict] = []
    for p in sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if p.name in _HIDDEN_NAMES or p.name.startswith(_HIDDEN_PREFIXES):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append({
            "path": str(p.relative_to(root)),
            "type": "dir" if p.is_dir() else "file",
            "size": 0 if p.is_dir() else st.st_size,
        })
        if len(entries) >= MAX_ENTRIES:
            break
    return entries


def read_file(project_id: str, relpath: str) -> Optional[dict]:
    root = workspace_root(project_id).resolve()
    p = safe_path(project_id, relpath)
    if not p.is_file():
        return None
    size = p.stat().st_size
    text, truncated, is_binary = _read_capped(p)
    if is_binary:
        return {"path": str(p.relative_to(root)),
                "content": "(binary file — not shown)",
                "size": size, "truncated": False, "binary": True}
    return {"path": str(p.relative_to(root)), "content": text,
            "size": size, "truncated": truncated, "binary": False}


# --------------------------------------------------------------------------
# 5 — the project's OWN Postgres (read-only schema + counts, never row data)
# --------------------------------------------------------------------------
# One fixed statement. Identifiers are quoted by Postgres itself via format(%I), so nothing
# from the (untrusted) project can be injected. query_to_xml keeps the count server-side.
_SCHEMA_SQL = """
SELECT c.relname,
       COALESCE((xpath('/row/cnt/text()', query_to_xml(
           format('select count(*) as cnt from %I.%I', n.nspname, c.relname),
           false, true, '')))[1]::text::bigint, 0) AS rows
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname = 'public'
ORDER BY c.relname LIMIT 200;
"""

_COLUMNS_SQL = """
SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position LIMIT 2000;
"""


async def _psql(project_id: str, sql: str, db_password: Optional[str]) -> tuple[int, str]:
    """Run a read-only query inside the project's own db container as the project's own DB
    role. Preferred path is the container-local unix socket (the postgres image's `local all
    all trust`) so the stored password never lands in a process argv; if that is refused we
    fall back to a password connection."""
    base = ["docker", "exec", f"{project_id}-db", "psql", "-U", "app", "-d", "app",
            "-X", "-q", "-t", "-A", "-F", "\x1f", "-v", "ON_ERROR_STOP=1", "-c", sql]
    rc, out = await run_argv(base, timeout=25)
    if rc == 0:
        return rc, out
    if db_password:
        rc2, out2 = await run_argv(
            ["docker", "exec", "-e", f"PGPASSWORD={db_password}", f"{project_id}-db",
             "psql", "-h", "127.0.0.1", "-U", "app", "-d", "app", "-X", "-q", "-t", "-A",
             "-F", "\x1f", "-v", "ON_ERROR_STOP=1", "-c", sql], timeout=25)
        if rc2 == 0:
            return rc2, out2
    return rc, out


async def database(project_id: str, db_password: Optional[str] = None) -> dict:
    """{tables:[{name,rows,columns:[{name,type,nullable}]}], migrations:[...]}.

    Empty lists (never an error) when the db container isn't up yet."""
    assert_shortid(project_id)
    migrations = []
    mig_dir = workspace_root(project_id) / "migrations"
    if mig_dir.is_dir():
        migrations = sorted(p.name for p in mig_dir.iterdir()
                            if p.is_file() and p.suffix == ".sql")[:200]

    if await _container_state(f"{project_id}-db") != "running":
        return {"tables": [], "migrations": migrations}

    rc, out = await _psql(project_id, _SCHEMA_SQL, db_password)
    if rc != 0:
        logger.info("db introspection for %s failed: %s", project_id, out[:200])
        return {"tables": [], "migrations": migrations}
    tables: dict[str, dict] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        try:
            rows = int(parts[1].strip())
        except ValueError:
            rows = 0
        tables[name] = {"name": name, "rows": rows, "columns": []}
        if len(tables) >= MAX_TABLES:
            break

    rc, out = await _psql(project_id, _COLUMNS_SQL, db_password)
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 4:
                continue
            tname = parts[0].strip()
            if tname not in tables:
                continue
            tables[tname]["columns"].append({
                "name": parts[1].strip(),
                "type": parts[2].strip(),
                "nullable": parts[3].strip().upper() == "YES",
            })
    return {"tables": list(tables.values()), "migrations": migrations}


# --------------------------------------------------------------------------
# 6 — secrets (masked by default)
# --------------------------------------------------------------------------
def mask_secret(value: str) -> str:
    """Show a short prefix only — enough to recognise, useless to an onlooker."""
    if not value:
        return "••••••"
    return value[:4] + "•" * 6


# --------------------------------------------------------------------------
# 7 — container logs
# --------------------------------------------------------------------------
async def logs(project_id: str, tail: int = 200) -> list[str]:
    assert_shortid(project_id)
    tail = max(1, min(int(tail or 200), MAX_LOG_LINES))
    rc, out = await run_argv(
        ["docker", "logs", "--tail", str(tail), f"{project_id}-app"], timeout=30)
    if rc != 0:
        return []
    return out.splitlines()[-tail:]


# --------------------------------------------------------------------------
# 8 — commits
# --------------------------------------------------------------------------
async def commits(project_id: str, owner: str, repo: str, limit: int = 30) -> list[dict]:
    ws = workspace_root(project_id)
    if not (ws / ".git").is_dir():
        return []
    limit = max(1, min(int(limit or 30), MAX_COMMITS))
    rc, out = await run_argv(
        ["git", "-C", str(ws), "log", f"-{limit}",
         "--pretty=format:%H\x1f%s\x1f%an\x1f%aI"], timeout=30)
    if rc != 0:
        return []
    items = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 4:
            continue
        full_sha = parts[0].strip()
        items.append({
            "sha": full_sha[:7],
            "message": parts[1].strip()[:500],
            "author": parts[2].strip()[:120],
            "date": parts[3].strip(),          # git %aI is already ISO-8601
            "url": f"{GITEA_URL}/{owner}/{repo}/commit/{full_sha}",
        })
    return items


# --------------------------------------------------------------------------
# 10 — QA rounds
# --------------------------------------------------------------------------
def qa_rounds(qa_entries: list[dict], qa_steps: list[dict]) -> list[dict]:
    """Fold the persisted QA messages (which carry the structured meta) together with the
    `runtime_qa` pipeline steps (which carry the human summary) into per-round records."""
    rounds: list[dict] = []
    for i, entry in enumerate(qa_entries):
        meta = entry.get("meta") or {}
        summary = entry.get("text") or ""
        if i < len(qa_steps) and qa_steps[i].get("log"):
            summary = qa_steps[i]["log"]
        server_errors = meta.get("server_errors")
        if not isinstance(server_errors, list):
            # Older runs persisted only failed network requests; surface those as the
            # server-side signal rather than inventing an empty list.
            server_errors = [str(n) for n in (meta.get("network") or [])][:20]
        rounds.append({
            "round": int(meta.get("rounds") or (i + 1)),
            "console_errors": [str(e) for e in (meta.get("errors") or [])][:20],
            "server_errors": server_errors,
            # phase 28: "a record was seeded through the API — did it render?" A round with
            # zero console errors and a failed flow is NOT a clean round.
            "flow_failures": [str(s) for s in (meta.get("semantic") or [])][:20],
            "flows_checked": int(meta.get("flows_checked") or 0),
            "flows_passed": int(meta.get("flows_passed") or 0),
            "summary": summary[:1000],
        })
    if not rounds:
        # No QA message rows (e.g. an older project) — fall back to the step logs alone.
        for i, st in enumerate(qa_steps):
            rounds.append({"round": i + 1, "console_errors": [], "server_errors": [],
                           "summary": (st.get("log") or "")[:1000]})
    return rounds


# --------------------------------------------------------------------------
# 11 — backlog
# --------------------------------------------------------------------------
_BUILD_STEP_RE = re.compile(r"^build_(\d+)$")


def backlog(project_id: str, steps: list[dict]) -> list[dict]:
    """Merge the TECHNICAL-PLAN build backlog (the titles) with the `build_NN` pipeline steps
    (the truth about what actually ran)."""
    from server.harness import backlog as backlog_mod

    titles: list[str] = []
    try:
        plan = workspace_root(project_id) / "docs" / "TECHNICAL-PLAN.md"
        if plan.is_file():
            text, _, is_bin = _read_capped(plan)
            if not is_bin:
                titles = backlog_mod.parse_backlog(text, cap=64)
    except Exception as e:  # noqa: BLE001
        logger.info("backlog parse for %s skipped: %s", project_id, e)

    by_idx: dict[int, dict] = {}
    for st in steps:
        m = _BUILD_STEP_RE.match(st.get("name") or "")
        if m:
            by_idx[int(m.group(1))] = st

    items: list[dict] = []
    total = max(len(titles), max(by_idx) if by_idx else 0)
    for n in range(1, total + 1):
        st = by_idx.get(n)
        title = titles[n - 1] if n <= len(titles) else ""
        if not title and st:
            # step logs read "built <feature> -> ['file', ...]"
            log = (st.get("log") or "")
            title = re.sub(r"^built\s+", "", log.split(" -> ")[0]).strip()
        status = (st or {}).get("status") or "pending"
        # `skipped` is a first-class outcome (phase 28) — a feature that could not be built
        # after a retry. Collapsing it to "pending" would let the UI imply it is still coming.
        if status not in ("done", "failed", "skipped"):
            status = "pending"
        item = {"idx": n, "title": (title or f"feature {n}")[:300], "status": status}
        if status == "skipped":
            item["reason"] = ((st or {}).get("log") or "")[:300]
        items.append(item)
    return items


# --------------------------------------------------------------------------
# 12 — routes (best-effort static scan of the app's source)
# --------------------------------------------------------------------------
_JS_ROUTE_RE = re.compile(
    r"""\b(?:app|router|api|server|r)\s*\.\s*(get|post|put|patch|delete|options|head|all)\s*"""
    r"""\(\s*['"`]([^'"`\n]{1,200})['"`]""", re.I)
_PY_ROUTE_RE = re.compile(
    r"""@\s*\w+\s*\.\s*(get|post|put|patch|delete|options|head)\s*\(\s*['"]([^'"\n]{1,200})['"]""",
    re.I)
_SCAN_DIRS = ("", "server", "src", "routes", "api", "lib", "app")
_SCAN_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".py")


def routes(project_id: str) -> list[dict]:
    root = workspace_root(project_id)
    if not root.is_dir():
        return []
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    scanned = 0
    for sub in _SCAN_DIRS:
        d = root / sub if sub else root
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if scanned >= MAX_SCAN_FILES:
                break
            if not p.is_file() or p.suffix not in _SCAN_SUFFIXES:
                continue
            if p.name in _HIDDEN_NAMES or p.name.startswith(_HIDDEN_PREFIXES):
                continue
            try:
                text, _, is_bin = _read_capped(p, SOURCE_SCAN_CAP)
            except OSError:
                continue
            if is_bin:
                continue
            scanned += 1
            for rx in (_JS_ROUTE_RE, _PY_ROUTE_RE):
                for m in rx.finditer(text):
                    method = m.group(1).upper()
                    path = m.group(2).strip()
                    if not path.startswith("/"):
                        continue
                    key = (method, path)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"method": method, "path": path})
    out.sort(key=lambda r: (r["path"], r["method"]))
    return out[:300]


# --------------------------------------------------------------------------
# 13 — container metrics
# --------------------------------------------------------------------------
_UNITS = {"b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12,
          "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}


def _to_bytes(text: str) -> int:
    m = re.match(r"^\s*([\d.]+)\s*([A-Za-z]+)\s*$", text or "")
    if not m:
        return 0
    try:
        return int(float(m.group(1)) * _UNITS.get(m.group(2).lower(), 1))
    except ValueError:
        return 0


async def metrics(project_id: str) -> list[dict]:
    assert_shortid(project_id)
    names = [f"{project_id}-app", f"{project_id}-db", f"{project_id}-redis"]
    states: dict[str, str] = {}
    for n in names:
        st = await _container_state(n)
        if st:
            states[n] = st
    if not states:
        return []
    running = [n for n, s in states.items() if s == "running"]
    stats: dict[str, dict] = {}
    if running:
        rc, out = await run_argv(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}"] + running,
            timeout=40)
        if rc == 0:
            for line in out.splitlines():
                try:
                    d = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if d.get("Name"):
                    stats[d["Name"]] = d
    out_list = []
    for n in names:
        if n not in states:
            continue
        d = stats.get(n, {})
        used, limit = 0, 0
        if d.get("MemUsage") and "/" in d["MemUsage"]:
            a, b = d["MemUsage"].split("/", 1)
            used, limit = _to_bytes(a), _to_bytes(b)
        try:
            cpu = float((d.get("CPUPerc") or "0").replace("%", "").strip())
        except ValueError:
            cpu = 0.0
        out_list.append({"name": n, "cpu_pct": cpu, "mem_used": used,
                         "mem_limit": limit, "status": states[n]})
    return out_list


# --------------------------------------------------------------------------
# 14 — redis cache
# --------------------------------------------------------------------------
async def cache(project_id: str) -> dict:
    assert_shortid(project_id)
    empty = {"dbsize": 0, "used_memory": 0, "keys": []}
    if await _container_state(f"{project_id}-redis") != "running":
        return empty
    base = ["docker", "exec", f"{project_id}-redis", "redis-cli"]
    rc, out = await run_argv(base + ["DBSIZE"], timeout=20)
    if rc != 0:
        return empty
    try:
        dbsize = int(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        dbsize = 0
    used = 0
    rc, out = await run_argv(base + ["INFO", "memory"], timeout=20)
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("used_memory:"):
                try:
                    used = int(line.split(":", 1)[1].strip())
                except ValueError:
                    used = 0
                break
    # SCAN (never KEYS — KEYS blocks the whole server) and stop after one bounded page.
    keys: list[str] = []
    rc, out = await run_argv(base + ["SCAN", "0", "COUNT", str(MAX_REDIS_KEYS)], timeout=20)
    if rc == 0:
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        keys = lines[1:][:MAX_REDIS_KEYS]      # line 0 is the cursor
    return {"dbsize": dbsize, "used_memory": used, "keys": keys}


# --------------------------------------------------------------------------
# 15 — public domain + TLS
# --------------------------------------------------------------------------
def _cert_facts(host: str) -> dict:
    """Blocking: fetch + parse the live leaf certificate. Called via a thread."""
    from cryptography import x509
    pem = ssl.get_server_certificate((host, 443), timeout=10)
    cert = x509.load_pem_x509_certificate(pem.encode())
    subject = ""
    try:
        cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        if cn:
            subject = cn[0].value
    except Exception:  # noqa: BLE001
        pass
    if not subject:
        try:
            san = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
            subject = san[0] if san else ""
        except Exception:  # noqa: BLE001
            subject = ""
    expires = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    return {"cert_subject": subject, "cert_expires": expires.isoformat()}


async def domain(project_id: str, subdomain: str) -> dict:
    assert_shortid(project_id)
    host = subdomain or f"{project_id}.{SITES_BASE}"
    info = {"url": f"https://{host}/", "subdomain": host,
            "cert_subject": None, "cert_expires": None}
    try:
        facts = await asyncio.wait_for(asyncio.to_thread(_cert_facts, host), timeout=20)
        info.update(facts)
    except Exception as e:  # noqa: BLE001
        logger.info("cert probe for %s failed: %s", host, e)
    return info


# --------------------------------------------------------------------------
# 16 — app container env (non-secret)
# --------------------------------------------------------------------------
def _mask_env_value(key: str, value: str) -> str:
    if _SECRETISH_KEY_RE.search(key):
        return mask_secret(value)
    # a credential embedded in a URL (postgres://app:pw@db/...) is a secret too
    return _URL_CRED_RE.sub(lambda m: f"://{m.group(1)}:••••••@", value)


async def env(project_id: str) -> list[dict]:
    assert_shortid(project_id)
    rc, out = await run_argv(
        ["docker", "inspect", "-f", "{{json .Config.Env}}", f"{project_id}-app"], timeout=20)
    if rc != 0:
        return []
    try:
        raw = json.loads(out.strip() or "[]")
    except Exception:  # noqa: BLE001
        return []
    items = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, str) or "=" not in entry:
            continue
        k, v = entry.split("=", 1)
        items.append({"key": k, "value": _mask_env_value(k, v)[:2000]})
        if len(items) >= 200:
            break
    return items


# --------------------------------------------------------------------------
# 17 — lifecycle
# --------------------------------------------------------------------------
LIFECYCLE_ACTIONS = ("stop", "start", "restart", "destroy")


async def reap_volumes(project_id: str) -> None:
    """Remove the project's on-disk data dirs after a destroy (absolute path, id-validated)."""
    from server.deployer import VOL_ROOT
    assert_shortid(project_id)
    d = Path(VOL_ROOT).resolve() / project_id
    if d.is_relative_to(Path(VOL_ROOT).resolve()) and d.exists():
        shutil.rmtree(d, ignore_errors=True)


async def observed_status(project_id: str) -> str:
    """The stack's real state after a lifecycle action (never a hopeful guess)."""
    st = await _container_state(f"{project_id}-app")
    if st is None:
        return "destroyed"
    return {"running": "running", "exited": "stopped", "created": "stopped",
            "paused": "paused"}.get(st, st)
