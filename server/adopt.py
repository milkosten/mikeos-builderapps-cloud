"""Phase 35 — ADOPT & EXTEND: turning somebody else's repository into this platform's app.

Three jobs, and the order they run in is the whole design:

    import_upstream()   clone the real history into the user's own Gitea, licence and all
    fit_contract()      add a thin ADAPTER so it satisfies the platform contract
    (then) deploy       the UNMODIFIED upstream, and get it green BEFORE any feature work

## The adapter is a SHIM, never a rewrite

Nothing under this module edits a single line of upstream source. Everything the platform
needs — `/health` in our exact shape, listening on `:3000`, the CSP that keeps the preview
iframe working, the migration runner — lives in a new `platform/` directory and a Dockerfile
that starts it. The upstream app runs either behind it (as a child process on an internal
port) or in front of it (as static files it serves).

That is not fastidiousness. It is what makes the next step honest: if we had "adapted" the
app by rewriting its entry point with an LLM and it then failed to boot, we would have no way
to tell whether we adopted a broken project or broke a working one. A shim keeps those two
failures distinguishable, and it keeps `git diff upstream..HEAD` readable by a human.

The adapter also gets its own `platform/package.json`, so express/pg/redis are installed
into their own tree and the upstream's dependency list is left exactly as its author wrote it.

## Two modes, decided from measurements

* **`proxy`** — the upstream has a server of its own. We start it as a child process with
  `PORT` pointing at an internal port and forward everything except `/health` to it. If the
  child dies, we say so on `/health` rather than pretending.
* **`static`** — the upstream is a front-end. We serve its built output (or its source
  directory, if it has no build step) with `express.static`, plus an SPA fallback.

Both modes probe at RUNTIME rather than trusting the guess made at build time: the static
root is whichever of the candidate directories actually exists in the image, because "the
build outputs to `dist/`" is a guess and a container that can check is better than a guess.

## Provenance is permanent and it is in three places

The upstream `LICENSE` is preserved untouched, a `NOTICE` names the source repo and the exact
commit, a workspace `doc` item records it on the project's board, and `projects.adopted`
records it in the platform's own database. A NOTICE file can be deleted by the next agent
that tidies the repo; a column cannot.

When the upstream ships NO LICENSE file — common, and true of the first repo this platform
ever adopted — the NOTICE says exactly that and names where the licence WAS declared
(`package.json`). We never write a LICENSE file on an author's behalf: granting a licence is
their act, and a file we invented would be the most dangerous kind of provenance, the kind
that looks authoritative.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from server import gitea, workspace

logger = logging.getLogger(__name__)

# A cap on what we will pull into the box. The scout already prefers small repos, but the
# thing that decides how much disk a stranger can spend is this number, not a preference.
MAX_CLONE_MB = int(os.environ.get("ADOPT_MAX_CLONE_MB", "600"))
CLONE_TIMEOUT = float(os.environ.get("ADOPT_CLONE_TIMEOUT", "600"))

_GITHUB_RE = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/?$")


class AdoptError(RuntimeError):
    """Something about the upstream made it un-adoptable. The message is shown to the user."""


async def _run(cmd: list[str], cwd: Optional[Path] = None,
               timeout: float = 300.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise AdoptError(f"timed out: {' '.join(cmd[:3])}")
    return proc.returncode, (out or b"").decode("utf-8", "replace")


def _redact(text: str) -> str:
    return re.sub(r"://[^/@\s]+:[^/@\s]+@", "://***:***@", text or "")


# ---------------------------------------------------------------------------
# 1. the import
# ---------------------------------------------------------------------------
async def import_upstream(project_id: str, *, gitea_user: str, repo: str, token: str,
                          upstream_url: str, author_name: str = "",
                          author_email: str = "") -> dict:
    """Clone the upstream WITH ITS HISTORY and push it into the user's empty Gitea repo.

    History, not a snapshot. A `git clone --depth 1` followed by a fresh `git init` would be
    faster and would produce a repo whose log claims we wrote a city simulator in one commit
    — which is a lie, and a lie that survives in the artefact forever. `git log --reverse`
    here starts with the upstream author's first commit, and the platform's own work sits on
    top of it where it belongs.

    The URL is validated against a github.com shape rather than trusted: it reaches this
    function from a scout document, and the one thing that must never be true is that a
    string from outside decides what gets cloned onto the box.
    """
    url = (upstream_url or "").strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if not _GITHUB_RE.match(url):
        raise AdoptError(f"{upstream_url!r} is not a github.com repository URL")
    clone_url = url + ".git"

    dst = workspace.path_for(project_id)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.parent.mkdir(parents=True, exist_ok=True)

    rc, out = await _run(["git", "clone", "--no-tags", "--single-branch", clone_url,
                          str(dst)], timeout=CLONE_TIMEOUT)
    if rc != 0:
        raise AdoptError(f"could not clone {url}: {_redact(out)[-400:]}")

    size_mb = _dir_mb(dst)
    if size_mb > MAX_CLONE_MB:
        shutil.rmtree(dst, ignore_errors=True)
        raise AdoptError(f"{url} is {size_mb} MB — too large to adopt "
                         f"(the limit is {MAX_CLONE_MB} MB)")

    rc, sha = await _run(["git", "rev-parse", "HEAD"], cwd=dst, timeout=30)
    upstream_commit = sha.strip() if rc == 0 else ""
    rc, branch = await _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=dst, timeout=30)
    upstream_branch = branch.strip() if rc == 0 else "main"
    rc, n = await _run(["git", "rev-list", "--count", "HEAD"], cwd=dst, timeout=60)
    commits = int(n.strip()) if rc == 0 and n.strip().isdigit() else 0

    # `upstream` is kept as a REMOTE, not deleted. It costs nothing and it means anyone can
    # later run `git fetch upstream && git log HEAD..upstream/HEAD` to see what the original
    # project has done since — which is the difference between a fork and a copy-paste.
    await _run(["git", "remote", "rename", "origin", "upstream"], cwd=dst, timeout=30)
    push_url = gitea.clone_url_for(gitea_user, repo, token)
    await _run(["git", "remote", "add", "origin", push_url], cwd=dst, timeout=30)
    await _run(["git", "branch", "-M", "main"], cwd=dst, timeout=30)
    await _run(["git", "config", "user.name", author_name or gitea_user], cwd=dst)
    await _run(["git", "config", "user.email",
                author_email or f"{gitea_user}@builderapps.osmike.com"], cwd=dst)
    rc, out = await _run(["git", "push", "-u", "origin", "main"], cwd=dst, timeout=600)
    if rc != 0:
        raise AdoptError(f"could not push the imported history: {_redact(out)[-400:]}")

    return {"upstream_url": url, "upstream_commit": upstream_commit,
            "upstream_branch": upstream_branch, "commits": commits, "size_mb": size_mb}


def _dir_mb(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
        if total > MAX_CLONE_MB * 1024 * 1024 * 2:
            break                    # already conclusive; do not walk a huge tree to the end
    return total // (1024 * 1024)


# ---------------------------------------------------------------------------
# 2. the adapter
# ---------------------------------------------------------------------------
_STATIC_CANDIDATES = ["dist", "build", "public", "out", "www", "site", "app", "."]


def survey(project_id: str) -> dict:
    """Read the checked-out upstream and decide how to run it. Measurements only."""
    root = workspace.path_for(project_id)
    pkg = {}
    pkg_path = root / "package.json"
    if pkg_path.is_file():
        try:
            pkg = json.loads(pkg_path.read_text("utf-8", "replace")[:400000])
        except Exception:  # noqa: BLE001
            pkg = {}
    scripts = {k: str(v) for k, v in (pkg.get("scripts") or {}).items()}

    listen_re = re.compile(r"\.listen\s*\(|createServer\s*\(", re.I)
    server_entry = ""
    for rel in ("server.js", "index.js", "app.js", "main.js", "server/index.js",
                "src/server.js", "src/index.js", "backend/server.js", "api/index.js",
                str(pkg.get("main") or "")):
        if not rel:
            continue
        p = root / rel
        if p.is_file() and p.stat().st_size < 2 * 1024 * 1024:
            if listen_re.search(p.read_text("utf-8", "replace")):
                server_entry = rel
                break

    start = scripts.get("start", "")
    has_build = bool(scripts.get("build"))
    # A `start` that is really a dev server (vite/webpack-dev-server/next dev) is NOT a
    # server we can proxy in production; treat it as a front-end and serve its build output.
    dev_start = bool(re.search(r"\b(vite|webpack-dev-server|parcel|serve|http-server|"
                               r"next\s+dev|nuxt\s+dev|react-scripts\s+start)\b", start))
    mode = "proxy" if (server_entry or (start and not dev_start)) else "static"

    static_roots = []
    if mode == "static":
        if has_build:
            # The build has not run yet, so this is the ORDER TO PROBE at runtime, not a
            # claim about what exists. `platform/server.cjs` picks the first that is really
            # there when the container starts.
            static_roots = ["dist", "build", "out", "public", "."]
        else:
            for d in _STATIC_CANDIDATES:
                p = root if d == "." else root / d
                if (p / "index.html").is_file():
                    static_roots.append(d)
            if not static_roots:
                static_roots = ["public", "dist", "."]

    node_engine = str(((pkg.get("engines") or {}).get("node") or "")).strip()
    node_major = "22"
    m = re.search(r"(\d{2})", node_engine)
    if m and int(m.group(1)) >= 18:
        node_major = m.group(1)

    return {
        "mode": mode,
        "has_package_json": bool(pkg),
        "package_manager": ("npm" if (root / "package-lock.json").is_file() else
                            "yarn" if (root / "yarn.lock").is_file() else
                            "pnpm" if (root / "pnpm-lock.yaml").is_file() else "npm"),
        "has_build": has_build,
        "build_script": scripts.get("build", ""),
        "start_script": start,
        "server_entry": server_entry,
        "static_roots": static_roots,
        "node_major": node_major,
        # ALWAYS inside platform/, and always `.cjs` — see the note above `_PLATFORM_PKG`.
        "migrate_path": "platform/migrate.cjs",
        "had_dockerfile": (root / "Dockerfile").is_file(),
        "had_compose": any((root / n).is_file() for n in
                           ("docker-compose.yml", "docker-compose.yaml", "compose.yml")),
    }


# ---- the shim ------------------------------------------------------------
# NOTE THE `.cjs` EXTENSIONS EVERYWHERE BELOW, AND DO NOT "TIDY" THEM AWAY.
#
# The adapter is CommonJS. Node decides a `.js` file's module system from the NEAREST
# package.json, and an adopted repo's own package.json is not ours to edit — SomCity's says
# `"type": "module"`, which made `db/migrate.js` load as ESM inside a CJS `require()` and the
# very first adopted app died on boot with `loadESMFromCJS`. A `"type": "commonjs"` in
# `platform/package.json` fixes the adapter's own directory and nothing else, so any file we
# write OUTSIDE it is still governed by the upstream's declaration.
#
# `.cjs` is unambiguous no matter what any package.json says, anywhere in the tree. It is the
# one thing here that cannot be broken by the next repository we adopt.
_PLATFORM_PKG = """{
  "name": "builderapps-platform-adapter",
  "private": true,
  "description": "The hosting platform's adapter. It wraps the adopted upstream app; it is not part of it.",
  "type": "commonjs",
  "main": "server.cjs",
  "dependencies": {
    "express": "^4.19.2",
    "pg": "^8.12.0",
    "redis": "^4.7.0"
  }
}
"""

_MIGRATE_JS = '''// Idempotent migration runner. Applies migrations/*.sql in name order, once each, tracked in
// _migrations. Convention shared across every MikeOS service — and shared, deliberately
// verbatim, with the template this platform builds from-scratch apps out of, so an adopted app
// and a generated one have exactly one migration story between them.
"use strict";
const fs = require("fs");
const path = require("path");

async function runMigrations(pool) {
  const dir = path.join(__dirname, "..", "migrations");
  await pool.query(
    "CREATE TABLE IF NOT EXISTS _migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
  );
  const files = fs.existsSync(dir)
    ? fs.readdirSync(dir).filter((f) => f.endsWith(".sql")).sort()
    : [];
  for (const f of files) {
    const done = await pool.query("SELECT 1 FROM _migrations WHERE name = $1", [f]);
    if (done.rowCount) continue;
    const sql = fs.readFileSync(path.join(dir, f), "utf8");
    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      await client.query(sql);
      await client.query("INSERT INTO _migrations (name) VALUES ($1)", [f]);
      await client.query("COMMIT");
      console.log(`[migrate] applied ${f}`);
    } catch (e) {
      await client.query("ROLLBACK");
      throw new Error(`migration ${f} failed: ${e.message}`);
    } finally {
      client.release();
    }
  }
}

module.exports = { runMigrations };
'''


def _platform_server_js(sv: dict) -> str:
    """The adapter. One file, no dependencies beyond express/pg/redis, and it never imports
    anything from the upstream — it either spawns it or serves it."""
    roots = json.dumps(sv.get("static_roots") or ["public", "dist", "."])
    mode = sv.get("mode")
    start_cmd = json.dumps(sv.get("start_script") or "")
    entry = json.dumps(sv.get("server_entry") or "")
    migrate_req = "./migrate.cjs"
    return f'''// ---------------------------------------------------------------------------
// THE PLATFORM ADAPTER — builderapps.
//
// This file belongs to the HOSTING PLATFORM, not to the app. The app in this repository was
// ADOPTED from an open-source project (see ../NOTICE) and its source is deliberately
// UNCHANGED. Everything the platform needs is here instead:
//
//   * GET /health in the exact shape the deployment gate reads
//   * listening on :3000
//   * the CSP that keeps the owner's preview iframe working
//   * running migrations/*.sql on boot
//
// Adapter mode for this app: {mode}
//
// If you are an agent adding a feature: add it to the UPSTREAM app, not here. This file
// exists so that the platform's requirements never have to be smeared through somebody
// else's codebase.
// ---------------------------------------------------------------------------
"use strict";

const express = require("express");
const fs = require("fs");
const http = require("http");
const path = require("path");
const {{ spawn }} = require("child_process");
const {{ Pool }} = require("pg");
const {{ createClient }} = require("redis");
const {{ runMigrations }} = require("{migrate_req}");

const PORT = parseInt(process.env.PORT || "3000", 10);
const UPSTREAM_PORT = parseInt(process.env.UPSTREAM_PORT || "3001", 10);
const ROOT = path.join(__dirname, "..");
const APP_TITLE = process.env.APP_TITLE || "builderapps app";
const MODE = process.env.ADAPTER_MODE || "{mode}";

const pool = new Pool({{ connectionString: process.env.DATABASE_URL }});
let redis = null;
async function getRedis() {{
  if (redis && redis.isOpen) return redis;
  redis = createClient({{ url: process.env.REDIS_URL || "redis://redis:6379" }});
  redis.on("error", () => {{}});
  await redis.connect();
  return redis;
}}

const app = express();

// The builder shows this app to its owner inside an iframe on https://builderapps.osmike.com.
// CSP `frame-ancestors` is the only header that can say "deny everyone except that one
// origin", so it is the only clickjacking control used here — and X-Frame-Options is
// deliberately never sent, because browsers enforce it IN ADDITION and it would silently
// override the allowance below.
const BUILDER_ORIGIN = "https://builderapps.osmike.com";
app.use((_req, res, next) => {{
  res.setHeader("Content-Security-Policy",
    "frame-ancestors 'self' " + BUILDER_ORIGIN);
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("Referrer-Policy", "same-origin");
  next();
}});

// ---- /health: the platform's, not the app's -------------------------------
// Reports what it actually checked. In proxy mode the upstream process is part of the
// answer: an adapter that is up while the app it wraps is dead must not report "ok", or the
// deployment gate would pass a blank page.
let upstreamUp = MODE !== "proxy";
let upstreamNote = "";
app.get("/health", async (_req, res) => {{
  const out = {{ status: "ok", db: "down", redis: "down" }};
  try {{ await pool.query("SELECT 1"); out.db = "ok"; }} catch {{ out.status = "degraded"; }}
  try {{ const r = await getRedis(); await r.ping(); out.redis = "ok"; }}
  catch {{ out.status = "degraded"; }}
  if (!upstreamUp) {{ out.status = "degraded"; out.app = upstreamNote || "upstream not running"; }}
  res.status(out.status === "ok" ? 200 : 503).json(out);
}});

// ---- mode: static ---------------------------------------------------------
function pickStaticRoot() {{
  // Probed at RUNTIME, not guessed at build time: the build has run by now, so we can simply
  // look. The order is most-specific-first.
  for (const rel of {roots}) {{
    const dir = path.resolve(ROOT, rel);
    if (fs.existsSync(path.join(dir, "index.html"))) return dir;
  }}
  return null;
}}

function serveStatic() {{
  const root = pickStaticRoot();
  if (!root) {{
    upstreamUp = false;
    upstreamNote = "no index.html found in " + JSON.stringify({roots});
    console.error("[adapter] " + upstreamNote);
    app.use((_req, res) => res.status(503).type("text/plain").send(
      "This app was adopted from an open-source project and the platform adapter could not "
      + "find its front-end. " + upstreamNote));
    return;
  }}
  console.log("[adapter] serving static root " + root);
  app.use(express.static(root, {{ index: "index.html", extensions: ["html"] }}));
  // SPA fallback — a client-routed app must not 404 on a deep link.
  app.get("*", (req, res, next) => {{
    if (req.method !== "GET" || req.path.startsWith("/api/")) return next();
    res.sendFile(path.join(root, "index.html"), (e) => (e ? next() : undefined));
  }});
}}

// ---- mode: proxy ----------------------------------------------------------
function startUpstream() {{
  const startScript = {start_cmd};
  const entry = {entry};
  const env = {{ ...process.env, PORT: String(UPSTREAM_PORT), HOST: "127.0.0.1",
                NODE_ENV: process.env.NODE_ENV || "production" }};
  let child;
  if (entry) {{
    console.log("[adapter] starting upstream: node " + entry);
    child = spawn("node", [entry], {{ cwd: ROOT, env, stdio: "inherit" }});
  }} else {{
    console.log("[adapter] starting upstream: npm start");
    child = spawn("npm", ["start"], {{ cwd: ROOT, env, stdio: "inherit" }});
  }}
  child.on("exit", (code, signal) => {{
    upstreamUp = false;
    upstreamNote = "the app process exited (code " + code + ", signal " + signal + ")";
    console.error("[adapter] " + upstreamNote);
  }});
  return child;
}}

function waitForUpstream(timeoutMs) {{
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve) => {{
    const tryOnce = () => {{
      const req = http.request(
        {{ host: "127.0.0.1", port: UPSTREAM_PORT, path: "/", method: "HEAD", timeout: 2000 }},
        () => {{ upstreamUp = true; resolve(true); }});
      req.on("error", () => {{
        if (Date.now() > deadline) return resolve(false);
        setTimeout(tryOnce, 500);
      }});
      req.on("timeout", () => {{ req.destroy(); }});
      req.end();
    }};
    tryOnce();
  }});
}}

function proxyAll() {{
  app.use((req, res) => {{
    const opts = {{ host: "127.0.0.1", port: UPSTREAM_PORT, path: req.originalUrl,
                   method: req.method, headers: {{ ...req.headers, host: "127.0.0.1:" + UPSTREAM_PORT }} }};
    const up = http.request(opts, (r) => {{
      res.writeHead(r.statusCode || 502, r.headers);
      r.pipe(res);
    }});
    up.on("error", (e) => {{
      if (!res.headersSent) res.status(502).type("text/plain").send(
        "the adopted app is not answering: " + e.message);
      else res.end();
    }});
    req.pipe(up);
  }});
}}

async function main() {{
  // Migrations first and always — an adopted app that grows a Postgres feature later must
  // find the same runner every generated app has.
  try {{ await runMigrations(pool); }}
  catch (e) {{ console.error("[adapter] migrations failed: " + e.message); throw e; }}

  if (MODE === "proxy") {{
    startUpstream();
    const ok = await waitForUpstream(60000);
    if (!ok) {{
      upstreamUp = false;
      upstreamNote = "the app did not start listening on :" + UPSTREAM_PORT + " within 60s";
      console.error("[adapter] " + upstreamNote);
    }}
    proxyAll();
  }} else {{
    serveStatic();
  }}

  app.listen(PORT, "0.0.0.0", () => {{
    console.log('[adapter] "' + APP_TITLE + '" on :' + PORT + " (mode " + MODE + ")");
  }});
}}

main().catch((e) => {{ console.error("[adapter] fatal: " + (e && e.message)); process.exit(1); }});
'''


def _dockerfile(sv: dict) -> str:
    node = sv.get("node_major") or "22"
    lines = [
        "# Built by builderapps for an ADOPTED open-source app (see NOTICE).",
        "#",
        "# The upstream project's own source is untouched. The platform's requirements live in",
        "# platform/ and are installed into their OWN dependency tree, so the upstream's",
        "# package.json is exactly as its author wrote it.",
        f"FROM node:{node}-bookworm-slim",
        "",
        "RUN apt-get update && apt-get install -y --no-install-recommends "
        "ca-certificates python3 make g++ git \\",
        "    && rm -rf /var/lib/apt/lists/*",
        "",
        "WORKDIR /app",
        "",
        "# 1. the platform adapter's dependencies (express/pg/redis) — cached separately from",
        "#    the upstream's, so a change to either does not invalidate the other's layer.",
        "COPY platform/package.json platform/package.json",
        "RUN cd platform && npm install --omit=dev --no-audit --no-fund && npm cache clean --force",
        "",
    ]
    if sv.get("has_package_json"):
        lines += [
            "# 2. the upstream's own dependencies, installed exactly as it declares them.",
            "COPY package.json package-lock.json* yarn.lock* pnpm-lock.yaml* ./",
            "RUN npm install --no-audit --no-fund || npm install --no-audit --no-fund "
            "--legacy-peer-deps",
            "",
        ]
    lines += ["# 3. the source.", "COPY . .", ""]
    if sv.get("has_build"):
        lines += [
            "# 4. the upstream's build step. It is run as its author wrote it; if it fails the",
            "#    image fails, which is the honest outcome — we would rather learn here than",
            "#    serve an empty page.",
            "RUN npm run build",
            "",
        ]
    lines += [
        "ENV PORT=3000 \\",
        "    UPSTREAM_PORT=3001 \\",
        f"    ADAPTER_MODE={sv.get('mode')} \\",
        "    NODE_ENV=production",
        "EXPOSE 3000",
        "",
        '# The ADAPTER is the entrypoint, never the upstream app: /health, the CSP and the',
        '# migrations are the platform\'s and must exist even if the app underneath is down.',
        'CMD ["node", "platform/server.cjs"]',
        "",
    ]
    return "\n".join(lines)


_COMPOSE = """# builderapps project stack for an ADOPTED app — Node + Postgres + Redis.
#
# Exactly three services (app/db/redis) because the deployer's normalizer accepts exactly
# those and rejects anything else. The upstream project's own docker-compose.yml, if it had
# one, is left in the repo untouched for reference but is NOT what runs here: it would have
# published ports, bind mounts and services this platform does not allow.
services:
  app:
    build: .
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_started
    environment:
      PORT: "3000"
      DATABASE_URL: postgresql://app:${DB_PASSWORD:-app_local}@db:5432/app
      REDIS_URL: redis://redis:6379
      APP_SECRET: ${APP_SECRET:-dev_secret_change_me}
      APP_TITLE: ${APP_TITLE:-Adopted builderapps app}
    healthcheck:
      test: ["CMD", "node", "-e", "fetch('http://localhost:3000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
      interval: 10s
      timeout: 5s
      retries: 18
      start_period: 90s

  db:
    image: postgres:18
    restart: unless-stopped
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: ${DB_PASSWORD:-app_local}
      POSTGRES_DB: app
      PGDATA: /var/lib/postgresql/data/pgdata
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app -d app"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 90s

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: ["redis-server", "--dir", "/data", "--stop-writes-on-bgsave-error", "no"]
    volumes:
      - redisdata:/data

volumes:
  pgdata:
  redisdata:
"""


def _notice(cand: dict, imp: dict, project_id: str, title: str,
            licence_file: str = "") -> str:
    lic = (cand.get("licence") or {}).get("spdx") or "see LICENSE"
    # TELL THE TRUTH ABOUT WHERE THE LICENCE LIVES. Plenty of real projects declare their
    # licence only in `package.json` and ship no LICENSE file (SomCity, the first repo this
    # platform ever adopted, is one). We must not write one on their behalf — granting a
    # licence is the author's act, not ours — and we must not claim to have preserved a file
    # that never existed.
    where = (f"The upstream `{licence_file}` file is preserved unmodified in this repository "
             f"and continues to apply to the upstream code."
             if licence_file else
             "The upstream project ships NO LICENSE FILE. Its licence is declared in "
             f"`package.json` as `{lic}`, which is where this was read from. We have "
             "deliberately NOT written a LICENSE file on the author's behalf — granting a "
             "licence is theirs to do, not ours. If this app matters commercially, ask the "
             "upstream author to add one.")
    return f"""NOTICE
======

This application is DERIVED WORK.

It was created by builderapps (https://builderapps.osmike.com) by adopting an existing
open-source project and adding features on top of it. The original project's code, its
commit history and its licence are all present in this repository — `git log --reverse`
begins with the original author's first commit, not ours.

  Upstream project : {cand.get('full_name') or ''}
  Upstream URL     : {imp.get('upstream_url') or cand.get('url') or ''}
  Upstream commit  : {imp.get('upstream_commit') or ''}
  Upstream branch  : {imp.get('upstream_branch') or ''}
  Licence          : {lic} (as found in {(cand.get('licence') or {}).get('source') or 'the repository'})
  Imported into    : builderapps project {project_id} ("{title}")

{where}

That licence is permissive ({lic}); it was checked before this project was adopted, and a
copyleft licence (GPL/AGPL/LGPL) would have been refused, because adopting one would have
imposed it on this application too.

WHAT WE CHANGED
---------------
The upstream source is deliberately UNMODIFIED by the adoption itself. Everything the hosting
platform requires was added alongside it, so that `git diff` against the upstream commit above
shows the platform adapter and nothing else:

  platform/server.cjs   the hosting adapter — GET /health, port 3000, the preview CSP, and
                        it starts or serves the upstream app without editing it
  platform/migrate.cjs  the shared migration runner (migrations/*.sql, applied on boot)
  platform/package.json the adapter's own dependencies, kept out of the upstream's tree
  Dockerfile            builds the upstream as its author intended, then runs the adapter
  docker-compose.yml    the platform's three-service stack (app/db/redis)

Anything committed AFTER the "chore: fit the platform contract" commit is work this platform
did on the owner's behalf.

The original project's authors did not endorse this application and are not responsible for
it. If you find this useful, the upstream project is the place to say thank you.
"""


async def fit_contract(project_id: str, cand: dict, imp: dict, title: str) -> dict:
    """Write the adapter. Returns the survey (what it decided and why).

    NOTHING HERE TOUCHES UPSTREAM SOURCE. Every path written is one the upstream did not
    have, with one deliberate exception: `Dockerfile` and `docker-compose.yml` are OVERWRITTEN
    when the upstream shipped its own, because those two files are the platform's contract
    with itself and an upstream compose with published ports and bind mounts would be rejected
    by the normalizer anyway. The originals stay in the git history, one commit back.
    """
    sv = survey(project_id)
    root = workspace.path_for(project_id)

    if sv["had_dockerfile"]:
        workspace.write_file(project_id, "docs/upstream-Dockerfile",
                             (root / "Dockerfile").read_text("utf-8", "replace")[:200000])
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml"):
        if (root / name).is_file():
            workspace.write_file(project_id, f"docs/upstream-{name}",
                                 (root / name).read_text("utf-8", "replace")[:200000])

    workspace.write_file(project_id, "platform/package.json", _PLATFORM_PKG)
    workspace.write_file(project_id, "platform/server.cjs", _platform_server_js(sv))
    workspace.write_file(project_id, sv["migrate_path"], _MIGRATE_JS)
    workspace.write_file(project_id, "Dockerfile", _dockerfile(sv))
    workspace.write_file(project_id, "docker-compose.yml", _COMPOSE)
    licence_file = next((n for n in sorted(os.listdir(root))
                         if re.match(r"^(LICEN[CS]E|COPYING|UNLICEN[CS]E)(\.[A-Za-z]+)?$",
                                     n, re.I)), "")
    workspace.write_file(project_id, "NOTICE",
                         _notice(cand, imp, project_id, title, licence_file))
    (root / "migrations").mkdir(parents=True, exist_ok=True)
    workspace.write_file(project_id, "migrations/.keep",
                         "-- migrations/*.sql are applied on boot by the platform adapter.\n")

    # A `.dockerignore` that would exclude `platform/` or `migrations/` from the build context
    # would produce an image that cannot start, with a confusing error. Rather than editing
    # the upstream's file, append an un-ignore for the two paths we added.
    di = root / ".dockerignore"
    if di.is_file():
        body = di.read_text("utf-8", "replace")
        if "!platform" not in body:
            workspace.write_file(project_id, ".dockerignore", body.rstrip("\n") + (
                "\n\n# builderapps: the platform adapter must reach the image.\n"
                "!platform\n!platform/**\n!migrations\n!migrations/**\n!NOTICE\n"))

    sv["licence_file"] = licence_file
    sv["licence_preserved"] = bool(licence_file)
    return sv
