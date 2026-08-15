#!/usr/bin/env python3
"""scout.py — the PRIOR-ART SCOUT (phase 35). Runs inside `mikeos-assistant-runtime`.

    "Cool that you want to build an online SimCity — there is already an open-source one.
     We could start from it and add your features instead of starting from zero."

Answering that honestly means doing real work on untrusted third-party code: searching
GitHub, cloning candidates, reading their `package.json`, finding their licence, deciding
whether there is a server or only static files, counting how big and how alive they are.
That is exactly the work that must not happen in the control plane, so it happens here —
`docker run --rm`, capped, no Docker socket, and (deliberately) **no credential of any kind
in the environment**. The container's whole output is one JSON document on stdout.

## What is NOT here, and why

* **No model call.** The container holds no model key and needs none: everything it decides
  is a measurement, not an opinion. The two judgement calls — *what to search for* and *how
  to say it to the user* — happen on the control plane, where the conversation already is.
  That split is what keeps a scout at ~$0.02 of thinking plus a minute of I/O.
* **No `mikeweb`.** The browser proxy is deliberately allow-listed to an assistant's OWN
  project (`server/browser_proxy.py`), so it cannot open github.com — and it would need a
  control-plane token in here to try. It is also unnecessary: we CLONE each candidate, which
  gives the real README and every other file, which is strictly more than a rendered page.
* **No `npm install` that runs code.** The one install we do is `--ignore-scripts`, and it
  proves exactly one thing, which is the thing we claim: *the dependency tree still
  resolves*. Lifecycle scripts from a stranger's repo are not run to produce a sales pitch.

## The verdict is EVIDENCE, not vibes

Every field in a candidate's record is something that was read off the repository. A star
count is in there because the user will look for it, but nothing is scored on it: the things
that decide `adopt` / `adopt-with-work` / `reject` are the licence, the runtime, whether
there is anything to serve, the size, and whether anyone has touched it this decade.

**Size is scored the "wrong" way round on purpose.** A 200k-LOC mature project is the worst
outcome, not the best: it looks impressive in a proposal and then neither the coding agent
nor the build pipeline can meaningfully change it. Small and hackable wins.
"""
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

GITHUB_API = "https://api.github.com"
UA = "builderapps-prior-art-scout"

MAX_CANDIDATES = int(os.environ.get("SCOUT_MAX_CANDIDATES", "6"))
DEADLINE_SEC = float(os.environ.get("SCOUT_DEADLINE_SEC", "210"))
CLONE_TIMEOUT = float(os.environ.get("SCOUT_CLONE_TIMEOUT", "90"))
NPM_TIMEOUT = float(os.environ.get("SCOUT_NPM_TIMEOUT", "100"))
VERIFY_INSTALL = os.environ.get("SCOUT_NPM", "1") not in ("0", "no", "false")
WORK = os.environ.get("SCOUT_WORKDIR", "/tmp/scout")

_T0 = time.monotonic()


def left() -> float:
    return DEADLINE_SEC - (time.monotonic() - _T0)


def log(msg: str) -> None:
    # stderr, never stdout: stdout carries the JSON document and nothing else.
    print(f"[scout +{time.monotonic() - _T0:6.1f}s] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------
def gh(path: str, timeout: float = 25.0):
    """One GitHub API call. Unauthenticated on purpose — see the module docstring: a token in
    here would be a token in a container that is about to hold a stranger's source tree."""
    url = path if path.startswith("http") else GITHUB_API + path
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        log(f"GET {url} -> HTTP {e.code} {body}")
        return {"_error": f"HTTP {e.code}", "_body": body}
    except Exception as e:  # noqa: BLE001
        log(f"GET {url} -> {e}")
        return {"_error": str(e)[:200]}


def search(queries):
    """Run the control plane's queries and merge the results.

    GitHub's search API is used rather than scraping because it can express the things that
    actually narrow a shortlist — `topic:`, `stars:>`, `language:`, `pushed:>` — and returns
    the licence and the last push in the same response.

    Unauthenticated search is 10 requests/minute. Four queries is well inside it, and a
    429/403 degrades to "fewer results", never to a crash: a scout that finds nothing is a
    silent miss, which costs nothing.
    """
    seen, out = {}, []
    for i, q in enumerate(queries):
        if left() < 40:
            log("out of time before query %d" % (i + 1))
            break
        qs = urllib.parse.urlencode({"q": q, "sort": "stars", "order": "desc",
                                     "per_page": "15"})
        data = gh(f"/search/repositories?{qs}")
        items = (data or {}).get("items") or []
        log(f"query {i+1}/{len(queries)}: {q!r} -> {len(items)} results")
        for it in items:
            full = it.get("full_name")
            if not full or full in seen:
                continue
            seen[full] = True
            out.append(it)
        if i < len(queries) - 1:
            time.sleep(6.5)          # 10 req/min unauthenticated; stay under it
    return out


def _prior(it: dict) -> float:
    """A cheap pre-rank so the expensive clone is spent on the plausible five.

    Not the verdict — the verdict comes from the clone. This only decides who gets looked at.
    """
    stars = float(it.get("stargazers_count") or 0)
    score = min(stars, 20000) ** 0.5
    pushed = str(it.get("pushed_at") or "")
    if pushed >= "2025":
        score += 40
    elif pushed >= "2023":
        score += 15
    if it.get("archived"):
        score -= 40
    if it.get("fork"):
        score -= 30
    # Repo size in KB. Penalised only GENTLY here, and deliberately: this number is mostly
    # checked-in ASSETS (a browser game with 400 MB of sprites is normal), whereas the thing
    # the verdict cares about is how much CODE there is — which is measured after the clone.
    # A hard pre-penalty on KB was keeping genuinely good candidates off the shortlist for
    # having nice artwork.
    size = float(it.get("size") or 0)
    if size > 1500000:
        score -= 40          # over ~1.5 GB the clone itself starts costing the whole minute
    elif size > 600000:
        score -= 10
    # THE PRIOR THAT MATTERS MOST HERE, and it is not stars: the runtime. This platform runs
    # Node, so a JS/TS repo is the only kind that can drop in — and without this the shortlist
    # is chosen almost entirely by popularity, which on any "game" search means five C++ and
    # Rust engines we will reject after paying to clone all five.
    lang = str(it.get("language") or "")
    if lang in ("JavaScript", "TypeScript"):
        score += 30
    elif lang in ("HTML", "CSS", "Svelte", "Vue"):
        score += 15           # a front-end we can put our own server in front of
    elif lang:
        score -= 15
    lic = ((it.get("license") or {}).get("spdx_id") or "").upper()
    if lic.startswith(("GPL", "AGPL", "LGPL")):
        score -= 25            # it will be rejected below; do not waste a clone slot on it
    return score


# ---------------------------------------------------------------------------
# licences — the gate
# ---------------------------------------------------------------------------
PERMISSIVE = {"MIT", "APACHE-2.0", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "ISC", "UNLICENSE",
              "0BSD", "CC0-1.0", "MIT-0", "BSD-4-CLAUSE", "ZLIB", "BSL-1.0"}
COPYLEFT = ("GPL", "AGPL", "LGPL", "MPL", "EPL", "CDDL", "OSL", "EUPL", "CC-BY-SA")

_LICENCE_PATTERNS = [
    (r"GNU AFFERO GENERAL PUBLIC LICENSE", "AGPL-3.0"),
    (r"GNU LESSER GENERAL PUBLIC LICENSE", "LGPL-3.0"),
    (r"GNU GENERAL PUBLIC LICENSE", "GPL"),
    (r"MOZILLA PUBLIC LICENSE", "MPL-2.0"),
    (r"APACHE LICENSE", "Apache-2.0"),
    (r"\bMIT LICEN[CS]E\b|PERMISSION IS HEREBY GRANTED, FREE OF CHARGE", "MIT"),
    (r"REDISTRIBUTION AND USE IN SOURCE AND BINARY FORMS.*3\. NEITHER THE NAME",
     "BSD-3-Clause"),
    (r"REDISTRIBUTION AND USE IN SOURCE AND BINARY FORMS", "BSD-2-Clause"),
    (r"THIS IS FREE AND UNENCUMBERED SOFTWARE RELEASED INTO THE PUBLIC DOMAIN",
     "Unlicense"),
    (r"ISC LICEN[CS]E|PERMISSION TO USE, COPY, MODIFY, AND/OR DISTRIBUTE", "ISC"),
]


def detect_licence(repo_dir: str, api_spdx: str, pkg: dict):
    """(spdx, where_we_found_it). Three sources, most trustworthy last.

    The licence FILE beats GitHub's guess and beats `package.json`, because that is the text
    that actually binds — and because the API happily reports `NOASSERTION` for a repo whose
    LICENSE file is a perfectly ordinary MIT.
    """
    found, src = "", ""
    spdx = (api_spdx or "").strip()
    if spdx and spdx.upper() not in ("NOASSERTION", "NONE", "NULL"):
        found, src = spdx, "github api"
    pj = str((pkg or {}).get("license") or "").strip()
    if pj and not found:
        found, src = pj, "package.json"
    for name in os.listdir(repo_dir) if os.path.isdir(repo_dir) else []:
        if not re.match(r"^(LICEN[CS]E|COPYING|UNLICEN[CS]E)(\.[A-Za-z]+)?$", name, re.I):
            continue
        try:
            with open(os.path.join(repo_dir, name), "r", encoding="utf-8",
                      errors="replace") as fh:
                head = fh.read(20000).upper()
        except OSError:
            continue
        for pat, spdx_name in _LICENCE_PATTERNS:
            if re.search(pat, head, re.S):
                # "GPL" needs its version, and v2 vs v3 changes nothing here (both reject)
                # but the user is owed the real name.
                if spdx_name == "GPL":
                    spdx_name = "GPL-2.0" if "VERSION 2" in head else "GPL-3.0"
                return spdx_name, f"{name} in the repo"
        return (found or "unknown"), f"{name} (unrecognised text)"
    return (found or ""), (src or "no licence file")


def licence_class(spdx: str) -> str:
    s = (spdx or "").upper().replace("_", "-")
    if not s or s in ("NOASSERTION", "UNKNOWN", "NONE"):
        return "none"
    if s in PERMISSIVE or s.rstrip("+") in PERMISSIVE:
        return "permissive"
    for c in COPYLEFT:
        if s.startswith(c):
            return "copyleft"
    return "unknown"


# ---------------------------------------------------------------------------
# inspecting a clone
# ---------------------------------------------------------------------------
SKIP_DIRS = {".git", "node_modules", "dist", "build", "out", "vendor", "third_party",
             ".next", ".nuxt", "coverage", "__pycache__", ".venv", "venv", "target",
             "bower_components", ".yarn", "assets/vendor"}
# The extension list is the MEASUREMENT, so a language missing from it does not merely go
# uncounted — it makes a 30k-line Elm project report "245 lines, barely more than a demo",
# which is a false statement in a proposal. The verdict would still be `reject` (wrong
# runtime), but for a reason that is not true. Breadth here is cheap; a wrong number is not.
CODE_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".css",
            ".scss", ".less", ".html", ".py", ".go", ".rs", ".rb", ".php", ".java",
            ".cs", ".sql", ".sh", ".c", ".cc", ".cpp", ".h", ".hpp", ".ex", ".exs",
            ".elm", ".kt", ".swift", ".dart", ".lua", ".gd", ".hs", ".clj", ".cljs",
            ".scala", ".zig", ".nim", ".ml", ".fs", ".pl", ".r", ".jl", ".glsl",
            ".frag", ".vert", ".wgsl", ".hlsl", ".m", ".mm"}
LANG_BY_EXT = {".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
               ".cjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
               ".py": "Python", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
               ".php": "PHP", ".java": "Java", ".cs": "C#", ".ex": "Elixir",
               ".exs": "Elixir", ".c": "C", ".cc": "C++", ".cpp": "C++", ".h": "C",
               ".elm": "Elm", ".kt": "Kotlin", ".swift": "Swift", ".dart": "Dart",
               ".lua": "Lua", ".gd": "GDScript", ".hs": "Haskell", ".clj": "Clojure",
               ".cljs": "Clojure", ".scala": "Scala", ".zig": "Zig", ".nim": "Nim",
               ".vue": "JavaScript", ".svelte": "JavaScript"}


def run(cmd, cwd=None, timeout=60.0):
    try:
        p = subprocess.run(cmd, cwd=cwd, timeout=timeout, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)[:400]


def clone(full_name: str, default_branch: str, dest: str):
    """Shallow but not one-deep: 120 commits is enough to say how alive a repo is, and still
    a few seconds. `--no-tags` because a decade of release tags is pure download."""
    url = f"https://github.com/{full_name}.git"
    rc, out = run(["git", "clone", "--depth", "120", "--single-branch", "--no-tags",
                   "--quiet", url, dest],
                  timeout=min(CLONE_TIMEOUT, max(20.0, left() - 15)))
    return rc == 0, out[-600:]


def walk_repo(repo_dir: str):
    """LOC, file count, per-language mix, and the paths we care about — in ONE walk.

    Bounded: 20k files and 2 MB per file. A repo with a checked-in 40 MB world texture must
    not be able to make the scout swap. (House rule: never read a whole unknown file into
    memory without a cap.)
    """
    loc = 0
    files = 0
    by_lang: dict = {}
    paths = set()
    biggest_dirs: dict = {}
    for root, dirs, names in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        rel_root = os.path.relpath(root, repo_dir)
        for n in names:
            rel = n if rel_root == "." else os.path.join(rel_root, n)
            paths.add(rel.replace("\\", "/"))
            ext = os.path.splitext(n)[1].lower()
            if ext not in CODE_EXT:
                continue
            if n.endswith((".min.js", ".min.css", "-min.js", ".bundle.js")):
                continue
            p = os.path.join(root, n)
            try:
                if os.path.getsize(p) > 2 * 1024 * 1024:
                    continue
                with open(p, "rb") as fh:
                    n_lines = fh.read().count(b"\n") + 1
            except OSError:
                continue
            files += 1
            loc += n_lines
            lang = LANG_BY_EXT.get(ext, "other")
            by_lang[lang] = by_lang.get(lang, 0) + n_lines
            top = rel_root.split(os.sep)[0] if rel_root != "." else "(root)"
            biggest_dirs[top] = biggest_dirs.get(top, 0) + n_lines
            if files > 20000:
                return loc, files, by_lang, paths, biggest_dirs
    return loc, files, by_lang, paths, biggest_dirs


def read_capped(path: str, cap: int = 400000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(cap)
    except OSError:
        return ""


_LISTEN_RE = re.compile(
    r"\.listen\s*\(|createServer\s*\(|express\s*\(\s*\)|new\s+Koa\s*\(|Fastify\s*\(", re.I)
# A framework's PRODUCTION start command is a server even though no file in the repo calls
# `.listen()` — `next start` and friends do it inside node_modules. Without this a Next.js app
# is measured as "nothing obvious to serve", which is both wrong and the difference between
# `adopt-with-work` and `reject`.
_FRAMEWORK_START_RE = re.compile(
    r"\b(next\s+start|nuxt\s+start|nuxi\s+preview|remix-serve|astro\s+preview|"
    r"node\s+build|vite\s+preview|nest\s+start|sails\s+lift|adonis\s+serve)\b", re.I)
_DB_SIGNS = [
    (r"\bpg\b|postgres|postgresql", "postgres"),
    (r"mongoose|mongodb", "mongo"),
    (r"better-sqlite3|sqlite3|\bsqlite\b", "sqlite"),
    (r"@prisma/client|prisma", "prisma"),
    (r"localStorage|IndexedDB|indexedDB|idb-keyval", "browser storage"),
    (r"lowdb|node-json-db", "json file"),
    (r"mysql2?|mariadb", "mysql"),
]


def inspect(repo_dir: str, meta: dict) -> dict:
    """Everything the verdict is allowed to be based on, read off the clone."""
    pkg = {}
    pkg_raw = read_capped(os.path.join(repo_dir, "package.json"), 200000)
    if pkg_raw:
        try:
            pkg = json.loads(pkg_raw)
        except Exception:  # noqa: BLE001
            pkg = {"_unparseable": True}
    scripts = {k: str(v)[:200] for k, v in (pkg.get("scripts") or {}).items()}
    deps = list((pkg.get("dependencies") or {}).keys())
    dev_deps = list((pkg.get("devDependencies") or {}).keys())

    loc, n_files, by_lang, paths, dir_mix = walk_repo(repo_dir)

    has_docker = "Dockerfile" in paths or "dockerfile" in paths
    has_compose = any(p in paths for p in
                      ("docker-compose.yml", "docker-compose.yaml", "compose.yml",
                       "compose.yaml"))

    # Is there a SERVER, or is this only files a browser loads? Our contract needs something
    # answering on :3000, so this decides which adapter the adopt-path would use — a shim in
    # front of a child process, or a static server. Both are shims; neither edits their code.
    server_files = []
    for rel in list(paths)[:6000]:
        if not rel.endswith((".js", ".mjs", ".cjs", ".ts")):
            continue
        if rel.startswith(("test/", "tests/", "spec/", "docs/", "examples/")):
            continue
        depth = rel.count("/")
        if depth > 2:
            continue
        body = read_capped(os.path.join(repo_dir, rel), 120000)
        if _LISTEN_RE.search(body):
            server_files.append(rel)
        if len(server_files) >= 6:
            break
    server_files.sort(key=lambda p: (p.count("/"), len(p)))

    entry = str(pkg.get("main") or "")
    start = scripts.get("start", "")
    build = scripts.get("build", "")
    static_roots = [d for d in ("public", "dist", "build", "static", "www", "site", "docs")
                    if f"{d}/index.html" in paths]
    if "index.html" in paths:
        static_roots.insert(0, ".")

    blob = " ".join(deps + dev_deps + list(scripts.values())) + " " + " ".join(
        sorted(p for p in paths if p.endswith((".js", ".ts", ".json")))[:400])
    data_layer = []
    for pat, label in _DB_SIGNS:
        if re.search(pat, blob, re.I):
            data_layer.append(label)
    # localStorage rarely shows in a filename; look in the source of the biggest files.
    if not data_layer:
        for rel in sorted(paths)[:200]:
            if rel.endswith((".js", ".ts", ".jsx", ".tsx")):
                if re.search(r"localStorage|indexedDB", read_capped(
                        os.path.join(repo_dir, rel), 80000)):
                    data_layer.append("browser storage")
                    break

    bundler = ""
    for name, label in (("vite.config", "vite"), ("webpack.config", "webpack"),
                        ("rollup.config", "rollup"), ("next.config", "next"),
                        ("esbuild", "esbuild"), ("parcel", "parcel")):
        if any(p.startswith(name) for p in paths) or name in blob:
            bundler = label
            break
    if not bundler and build:
        bundler = build.split()[0][:24]

    rc, last = run(["git", "log", "-1", "--format=%cI|%h|%s"], cwd=repo_dir, timeout=20)
    last_commit_iso, last_sha, last_subject = "", "", ""
    if rc == 0 and "|" in last:
        parts = last.strip().split("|", 2)
        last_commit_iso, last_sha = parts[0], parts[1]
        last_subject = parts[2][:160] if len(parts) > 2 else ""
    rc, cnt = run(["git", "rev-list", "--count", "HEAD"], cwd=repo_dir, timeout=20)
    depth_seen = int(cnt.strip()) if rc == 0 and cnt.strip().isdigit() else 0
    rc, year = run(["git", "log", "--since=1.year", "--oneline"], cwd=repo_dir, timeout=20)
    commits_last_year = len([x for x in year.splitlines() if x.strip()]) if rc == 0 else 0

    readme = ""
    for cand in ("README.md", "readme.md", "README.MD", "README", "README.rst"):
        if cand in paths:
            readme = read_capped(os.path.join(repo_dir, cand), 8000)
            break

    langs = sorted(by_lang.items(), key=lambda kv: -kv[1])
    node_loc = by_lang.get("JavaScript", 0) + by_lang.get("TypeScript", 0)
    return {
        "loc": loc,
        "files": n_files,
        "languages": [{"lang": k, "loc": v} for k, v in langs[:5]],
        "primary_language": langs[0][0] if langs else "",
        "node_share": round(node_loc / loc, 2) if loc else 0.0,
        "biggest_dirs": sorted(
            [{"dir": k, "loc": v} for k, v in dir_mix.items()],
            key=lambda d: -d["loc"])[:5],
        "has_package_json": bool(pkg_raw),
        "package_name": str(pkg.get("name") or "")[:80],
        "dependencies": len(deps),
        "dev_dependencies": len(dev_deps),
        "top_dependencies": deps[:12],
        "scripts": {k: scripts[k] for k in ("start", "build", "dev", "serve", "preview")
                    if k in scripts},
        "entry": entry,
        "has_dockerfile": has_docker,
        "has_compose": has_compose,
        "server_files": server_files[:4],
        "framework_server": bool(_FRAMEWORK_START_RE.search(start)),
        "has_server": (bool(server_files) or bool(start and "node" in start)
                       or bool(_FRAMEWORK_START_RE.search(start))),
        "static_roots": static_roots[:3],
        "static_only": (not server_files) and bool(static_roots),
        "build_step": bool(build),
        "bundler": bundler,
        "data_layer": data_layer or ["none found"],
        "last_commit": last_commit_iso,
        "last_commit_sha": last_sha,
        "last_commit_subject": last_subject,
        "commits_seen": depth_seen,
        "commits_last_year": commits_last_year,
        "readme_head": readme[:1800],
    }


def verify_install(repo_dir: str) -> dict:
    """`npm install --ignore-scripts`. Proves the dependency tree still RESOLVES — nothing
    more, and the result says exactly that.

    `--ignore-scripts` is not a performance flag. It is the line between "we measured
    something" and "we executed a stranger's postinstall hook to write a sales pitch".
    """
    if not os.path.exists(os.path.join(repo_dir, "package.json")):
        return {"attempted": False, "reason": "no package.json"}
    t0 = time.monotonic()
    rc, out = run(["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund",
                   "--loglevel", "error"],
                  cwd=repo_dir, timeout=min(NPM_TIMEOUT, max(20.0, left() - 10)))
    return {"attempted": True, "ok": rc == 0, "seconds": round(time.monotonic() - t0, 1),
            "detail": ("dependencies resolve" if rc == 0
                       else out.strip().splitlines()[-1][:240] if out.strip()
                       else f"npm exited {rc}")}


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------
def score(cand: dict) -> dict:
    """Deterministic. Every point added or removed carries the sentence that explains it, so
    the proposal the user reads is assembled from measurements rather than from adjectives."""
    ev = cand["evidence"]
    lic = cand["licence"]
    notes, points = [], 0
    penalties: list = []          # (points_lost, the note) — what a `reject` is REALLY about

    # --- THE GATE -----------------------------------------------------------
    cls = licence_class(lic["spdx"])
    if cls == "copyleft":
        return {"verdict": "reject", "score": 0, "blocking": "licence",
                "why": (f"{lic['spdx']} is copyleft — adopting it would make YOUR app "
                        f"{lic['spdx']} too, forever. We only start from permissive "
                        "licences (MIT, Apache-2.0, BSD, ISC, Unlicense)."),
                "notes": [f"licence: {lic['spdx']} ({lic['source']}) — copyleft, rejected"]}
    if cls in ("none", "unknown"):
        return {"verdict": "reject", "score": 0, "blocking": "licence",
                "why": ("no usable licence" if cls == "none" else
                        f"licence {lic['spdx']!r} is not one we recognise as permissive")
                       + " — without a permissive licence you would have no right to ship "
                         "this as your own app.",
                "notes": [f"licence: {lic['spdx'] or 'none found'} ({lic['source']})"]}
    points += 25
    notes.append(f"licence: {lic['spdx']} ({lic['source']}) — permissive, safe to adopt")

    # --- runtime ------------------------------------------------------------
    lang = ev.get("primary_language") or "unknown"
    if ev.get("node_share", 0) >= 0.5 or lang in ("JavaScript", "TypeScript"):
        points += 20
        notes.append(f"stack: {lang} — same runtime as the platform (Node), drop-in")
    elif lang in ("Python", "Go", "Rust", "Ruby", "PHP", "Java", "C#", "Elixir"):
        points -= 25
        notes.append(f"stack: {lang} — a different runtime from our Node contract; the app "
                     "would have to be containerised differently")
        penalties.append((-25, notes[-1]))
    else:
        notes.append(f"stack: {lang or 'unclear'}")

    # --- is there anything to serve? ---------------------------------------
    if ev.get("has_server"):
        points += 15
        notes.append("has its own server (" + (", ".join(ev.get("server_files") or [])
                                               or ("`" + (ev.get("scripts") or {}).get(
                                                   "start", "a start script") + "`")) + ")")
    elif ev.get("static_only"):
        points += 10
        notes.append("static front-end (" + ", ".join(ev.get("static_roots") or ["/"])
                     + ") — we put our server in front of it, no backend of its own yet")
    else:
        points -= 20
        notes.append("nothing obvious to serve — no server file and no index.html")
        penalties.append((-20, notes[-1]))

    # --- SIZE. Small and hackable beats big and impressive. ------------------
    loc = int(ev.get("loc") or 0)
    if loc == 0:
        points -= 20
        notes.append("no source found — nothing to build on")
        penalties.append((-20, notes[-1]))
    elif loc < 400:
        points -= 10
        notes.append(f"~{loc:,} lines — barely more than a demo; little is actually reused")
        penalties.append((-10, notes[-1]))
    elif loc <= 25000:
        points += 20
        notes.append(f"~{loc:,} lines across {ev.get('files')} files — small enough that we "
                     "can genuinely change it")
    elif loc <= 60000:
        points += 5
        notes.append(f"~{loc:,} lines — large; changes will be slower and more careful")
    else:
        points -= 25
        notes.append(f"~{loc:,} lines — too big to reason about; adopting it would mean "
                     "neither we nor you could meaningfully change it")
        penalties.append((-25, notes[-1]))

    # --- aliveness ----------------------------------------------------------
    last = str(ev.get("last_commit") or "")
    if cand.get("archived"):
        points -= 15
        notes.append("archived by its author — no upstream fixes are coming")
        penalties.append((-15, notes[-1]))
    if last >= "2025":
        points += 15
        notes.append(f"last commit {last[:10]} — actively maintained")
    elif last >= "2023":
        points += 5
        notes.append(f"last commit {last[:10]}")
    elif last:
        points -= 10
        notes.append(f"last commit {last[:10]} — dormant for years")
        penalties.append((-10, notes[-1]))
    if ev.get("commits_last_year"):
        notes.append(f"{ev['commits_last_year']} commits in the last year")

    # --- build + data -------------------------------------------------------
    if ev.get("has_dockerfile"):
        points += 5
        notes.append("ships a Dockerfile")
    if ev.get("build_step"):
        notes.append(f"needs a build step ({ev.get('bundler') or 'npm run build'}) — "
                     "fine, it changes our Dockerfile")
    dl = ev.get("data_layer") or []
    if "postgres" in dl:
        points += 10
        notes.append("stores data in Postgres — exactly our data layer")
    elif "browser storage" in dl or dl == ["none found"]:
        notes.append("no server-side database yet — persistence is a feature we would add")
    else:
        points -= 5
        notes.append("data layer: " + ", ".join(dl) + " — a migration decision")
        penalties.append((-5, notes[-1]))

    inst = cand.get("install") or {}
    if inst.get("attempted"):
        if inst.get("ok"):
            points += 5
            notes.append(f"`npm install --ignore-scripts` succeeded in {inst['seconds']}s — "
                         "dependencies still resolve")
        else:
            points -= 15
            notes.append("`npm install --ignore-scripts` FAILED — " + str(inst.get("detail")))
            penalties.append((-15, notes[-1]))

    deps = int(ev.get("dependencies") or 0)
    notes.append(f"{deps} direct dependencies"
                 + (" — a large tree to inherit" if deps > 40 else ""))

    verdict = "adopt" if points >= 70 else ("adopt-with-work" if points >= 45 else "reject")
    why = ""
    if verdict == "reject":
        # THE REASON MUST BE THE REASON. Quoting the last three notes made a repo get rejected
        # "because 146 commits in the last year", which is a fact in its favour — the sort of
        # nonsense that makes a user stop believing any of the evidence. Quote what actually
        # cost it the points.
        worst = [n for _, n in sorted(penalties, key=lambda pn: pn[0])[:3]]
        why = ("the evidence does not support starting from this: " + "; ".join(worst)
               if worst else "nothing here scored well enough to be worth starting from")
    return {"verdict": verdict, "score": points, "blocking": "", "why": why, "notes": notes}


def headline(cand: dict) -> str:
    """The one line a human can judge — the thing the phase brief asked for instead of a
    star count."""
    ev = cand["evidence"]
    bits = [cand["licence"]["spdx"]]
    lang = ev.get("primary_language")
    if lang:
        bits.append(lang + (" + a server" if ev.get("has_server") else
                            " (static front-end)" if ev.get("static_only") else ""))
    if ev.get("has_dockerfile"):
        bits.append("has a Dockerfile")
    if ev.get("last_commit"):
        bits.append("last commit " + ev["last_commit"][:10])
    if ev.get("loc"):
        bits.append(f"~{int(ev['loc']):,} LOC")
    if ev.get("dependencies"):
        bits.append(f"{ev['dependencies']} deps")
    if float(cand.get("repo_mb") or 0) > 300:
        bits.append(f"{cand['repo_mb']:.0f} MB checkout")
    return ", ".join(b for b in bits if b)


# ---------------------------------------------------------------------------
# one candidate, end to end
# ---------------------------------------------------------------------------
def evaluate(item: dict, workdir: str) -> dict:
    full = item.get("full_name") or ""
    dest = os.path.join(workdir, full.replace("/", "__"))
    cand = {
        "full_name": full,
        "url": item.get("html_url") or f"https://github.com/{full}",
        "description": (item.get("description") or "")[:400],
        "stars": int(item.get("stargazers_count") or 0),
        "forks": int(item.get("forks_count") or 0),
        "open_issues": int(item.get("open_issues_count") or 0),
        "archived": bool(item.get("archived")),
        "is_fork": bool(item.get("fork")),
        "topics": (item.get("topics") or [])[:10],
        "pushed_at": item.get("pushed_at") or "",
        "default_branch": item.get("default_branch") or "main",
        "repo_mb": round(float(item.get("size") or 0) / 1024, 1),
    }
    ok, out = clone(full, cand["default_branch"], dest)
    if not ok:
        cand.update({"licence": {"spdx": "", "source": "clone failed"},
                     "evidence": {}, "clone_error": out,
                     "verdict": "reject", "score": 0,
                     "why": "could not clone the repository", "notes": [], "headline": ""})
        return cand
    pkg = {}
    raw = read_capped(os.path.join(dest, "package.json"), 200000)
    if raw:
        try:
            pkg = json.loads(raw)
        except Exception:  # noqa: BLE001
            pkg = {}
    spdx, source = detect_licence(dest, ((item.get("license") or {}).get("spdx_id") or ""),
                                  pkg)
    cand["licence"] = {"spdx": spdx, "source": source,
                       "class": licence_class(spdx)}
    cand["evidence"] = inspect(dest, item)
    cand["head_sha"] = cand["evidence"].get("last_commit_sha") or ""
    # The install check is only spent on candidates that could actually be proposed: a
    # copyleft repo is already rejected, and paying 90 seconds to learn its lockfile is fine
    # would be 90 seconds of the user's minute spent on an answer nobody will use.
    if VERIFY_INSTALL and licence_class(spdx) == "permissive" and left() > 60:
        cand["install"] = verify_install(dest)
    cand.update(score(cand))
    cand["headline"] = headline(cand)
    shutil.rmtree(dest, ignore_errors=True)
    return cand


def main() -> int:
    try:
        queries = json.loads(os.environ.get("SCOUT_QUERIES") or "[]")
    except Exception:  # noqa: BLE001
        queries = []
    queries = [str(q)[:200] for q in queries if str(q).strip()][:5]
    result = {"ok": False, "queries": queries, "candidates": [], "considered": 0,
              "error": "", "seconds": 0.0}
    if not queries:
        result["error"] = "no queries"
        emit(result)
        return 0

    os.makedirs(WORK, exist_ok=True)
    items = search(queries)
    result["considered"] = len(items)
    items.sort(key=_prior, reverse=True)
    short = items[:MAX_CANDIDATES]
    log(f"shortlist: {[i.get('full_name') for i in short]}")

    with tempfile.TemporaryDirectory(dir=WORK) as td:
        # Cloning is I/O, so it parallelises well; the inspection after it is cheap. Three at
        # a time keeps the tmpfs and the pids-limit comfortable.
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futs = {pool.submit(evaluate, it, td): it for it in short}
            for f in concurrent.futures.as_completed(futs, timeout=max(30.0, left())):
                try:
                    result["candidates"].append(f.result())
                except Exception as e:  # noqa: BLE001
                    it = futs[f]
                    log(f"evaluate {it.get('full_name')} failed: {e}")

    order = {"adopt": 0, "adopt-with-work": 1, "reject": 2}
    result["candidates"].sort(key=lambda c: (order.get(c.get("verdict"), 3),
                                             -int(c.get("score") or 0)))
    result["ok"] = True
    result["seconds"] = round(time.monotonic() - _T0, 1)
    emit(result)
    return 0


def emit(result: dict) -> None:
    """Fenced, so a git warning or an npm notice on stdout cannot corrupt the document."""
    sys.stdout.write("<<<SCOUT_JSON>>>\n")
    sys.stdout.write(json.dumps(result))
    sys.stdout.write("\n<<<END_SCOUT_JSON>>>\n")
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        log(f"fatal: {e}")
        emit({"ok": False, "error": str(e)[:300], "candidates": [],
              "seconds": round(time.monotonic() - _T0, 1)})
        sys.exit(0)
