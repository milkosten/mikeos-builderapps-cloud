"""LLM codegen helpers for the create/update pipelines (phases 13/15/18).

Kimi (via server.gpu.chat) writes the *content*: the strategy docs, the schema, and each
feature's file set. Everything structural (which files, the compose, the deploy, git) stays
deterministic in the pipeline. Two hard-won rules are baked in here:

* **The no-external-SaaS rule** — a HARD system-prompt constraint repeated into every codegen
  call: the plan/app must be fully self-hosted on Node+Postgres+Redis. No Auth0/Stripe/
  cookie-consent/analytics/email SaaS — we build all of it ourselves.
* **The truncation guard (the "Kimi bug")** — a file that comes back without its closing
  fence, or empty, is DISCARDED and retried with a larger token budget scaled to the file's
  size. We never write a half-file to disk.

All model output is parsed defensively (strip code fences, tolerate prose around JSON).
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from server import gpu

logger = logging.getLogger(__name__)

# The constraint that must appear in EVERY codegen system prompt.
NO_SAAS_RULE = (
    "ABSOLUTE ARCHITECTURE RULES (never violate):\n"
    "1. The entire app is self-hosted on Node.js (Express) + PostgreSQL + Redis. Nothing else.\n"
    "2. NEVER propose, import, call, or integrate ANY external or paid third-party service: "
    "no Auth0/Clerk/Firebase auth, no Stripe/PayPal, no SendGrid/Mailgun/SMTP SaaS, no "
    "Google/Plausible/Segment analytics, no cookie-consent SaaS, no external CDNs, no hosted "
    "search, no S3/cloud storage. If a feature needs auth, email, analytics, payments, consent, "
    "search, or file storage, we BUILD IT OURSELVES on Postgres/Redis/Node.\n"
    "3. The frontend is plain self-contained HTML/CSS/JS served from the app's own public/ "
    "directory. NO external <script src> or <link href> to any CDN, no web fonts from a remote "
    "host, no remote images. Everything is inlined or served locally.\n"
    "4. All SQL is parameterized ($1,$2,...). Migrations are idempotent (IF NOT EXISTS). Never "
    "use a reserved SQL keyword as a column name. Timestamps are timestamptz / ISO-8601.\n"
    "5. Cap request body sizes; never read an unbounded blob into memory.\n"
    "6. PLATFORM CONTRACT — `GET /health` belongs to the hosting platform, NOT to the app. It MUST "
    "always exist and MUST respond with exactly this shape: "
    "{\"status\":\"ok\",\"db\":\"ok\",\"redis\":\"ok\"} (each value is \"ok\" only when that datastore "
    "actually answered; use \"down\" otherwise). Never rename it, never change those field names, and "
    "never drop it when you rewrite server.js. The deployment health-gate reads this exact shape — "
    "change it and a perfectly working app gets reported as FAILED and never finishes building.\n"
    "7. Keep client and server response shapes consistent. When the frontend consumes one of your "
    "own JSON endpoints, unwrap the object before iterating: an endpoint returning {\"items\":[...]} "
    "must be read as `data.items`, never iterated directly — `data.forEach(...)` on an object throws "
    "\"forEach is not a function\" and the list silently never renders.\n"
    "8. MIGRATIONS — the scaffold ALREADY has a working, idempotent migration runner: `db/migrate.js` "
    "applies every `migrations/*.sql` once, in filename order, tracking them in a `_migrations` table, "
    "and `server.js` calls it on boot. DO NOT write your own migration runner and DO NOT create a "
    "second tracking table (e.g. `schema_migrations`) — a hand-rolled one has already shipped broken "
    "(`ON CONFLICT (filename)` with no UNIQUE constraint), which crash-loops the app at boot and fails "
    "the whole build. To add schema, ONLY add a new numbered file like `migrations/002_feature.sql`. "
    "Create each table exactly once across all migrations (never both `notes` and `public.notes`), "
    "always `IF NOT EXISTS`, and never ALTER or re-shape the scaffold's existing `app_meta` table. Any "
    "`ON CONFLICT (col)` you write REQUIRES a matching UNIQUE/PRIMARY KEY constraint on that column."
)


def _strip_fences(text: str) -> str:
    """Remove a leading/trailing ``` fence (with optional language tag) if present."""
    t = text.strip()
    if t.startswith("```"):
        # drop first line (```lang) and a trailing ```
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _extract_json(text: str) -> Any:
    """Best-effort: pull the first JSON object/array out of a model reply."""
    t = _strip_fences(text)
    try:
        return json.loads(t)
    except Exception:
        pass
    # find the outermost {...} or [...]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i = t.find(open_c)
        j = t.rfind(close_c)
        if i != -1 and j != -1 and j > i:
            frag = t[i:j + 1]
            try:
                return json.loads(frag)
            except Exception:
                continue
    raise ValueError("no parseable JSON in model reply")


# ---- phase 13: strategy documents -----------------------------------------
_DOC_SPECS: List[Dict[str, str]] = [
    {"file": "VISION.md", "title": "Vision, Mission & Objective",
     "ask": "Write the Vision (the world we want), the Mission (what this product does about it), "
            "and 3-5 concrete measurable Objectives. Be specific to THIS app, not generic."},
    {"file": "ICP.md", "title": "Ideal Customer Profile",
     "ask": "Define the Ideal Customer Profile: who they are, their context, the job-to-be-done, "
            "their pains and gains, and where they are today. Specific segments, not 'everyone'."},
    {"file": "UX.md", "title": "User Experience",
     "ask": "Describe the User Experience: the primary user flows step by step, the screens/pages, "
            "the key UI states (empty, loading, error, success), and the interaction details. This "
            "doc drives the build AND the runtime QA, so list each flow a tester could click through."},
    {"file": "BUYER-PERSONA.md", "title": "Ideal Buyer Persona",
     "ask": "Write one vivid Ideal Buyer Persona: name, role, goals, frustrations, a day in their "
            "life, and why this product wins them over."},
    {"file": "MARKETING.md", "title": "Marketing Plan",
     "ask": "A concrete marketing plan: positioning, the core message, 3 channels with tactics, and "
            "a simple launch sequence. All self-hosted (our own landing page, our own email — no SaaS)."},
]

TECH_PLAN_ASK = (
    "Write TECHNICAL-PLAN.md for this app on Node.js (Express) + PostgreSQL + Redis, FULLY "
    "self-hosted. It MUST contain these sections with these exact H2 headings:\n"
    "## Architecture — a short paragraph.\n"
    "## Data Model — every table with its columns and types (timestamptz for times; no reserved "
    "keyword columns; explicit primary keys).\n"
    "## Routes — every HTTP route as a bullet `METHOD /path — what it does`.\n"
    "## Pages — every frontend page/view as a bullet `/path — what the user sees`.\n"
    "## Build Backlog — an ORDERED numbered list of 6-14 concrete build tasks, each ONE small "
    "feature (e.g. `1. Notes table migration + pool wiring`, `2. POST /api/notes create endpoint`, "
    "`3. GET /api/notes list newest-first`, `4. Frontend: add-note form + list render`, "
    "`5. DELETE /api/notes/:id + remove-from-list`). Each item must be independently buildable and "
    "testable. Keep the app SMALL and correct; do not over-scope."
)


async def write_strategy_docs(brief: str, title: str) -> Dict[str, str]:
    """Generate the six strategy docs. Returns {relative_path: markdown}. Each doc is one
    focused call so the model stays specific and we stay within a small token budget."""
    out: Dict[str, str] = {}
    sys = ("You are the founding product+engineering lead for a new web app. You write crisp, "
           "specific strategy docs — never generic filler.\n\n" + NO_SAAS_RULE)
    for spec in _DOC_SPECS:
        user = (f"App title: {title}\nApp brief: {brief}\n\n{spec['ask']}\n\n"
                f"Output ONLY the Markdown body of docs/{spec['file']} starting with "
                f"`# {spec['title']}`. No preamble, no code fences.")
        md = await gpu.chat(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.6, num_predict=2200, timeout=300,
        )
        out[f"docs/{spec['file']}"] = _strip_fences(md).strip() + "\n"
    # Technical plan last (it references the same brief; drives the backlog).
    user = (f"App title: {title}\nApp brief: {brief}\n\n{TECH_PLAN_ASK}\n\n"
            f"Output ONLY the Markdown body of docs/TECHNICAL-PLAN.md starting with "
            f"`# Technical Plan`. No preamble, no code fences.")
    tp = await gpu.chat(
        [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        temperature=0.4, num_predict=3000, timeout=360,
    )
    out["docs/TECHNICAL-PLAN.md"] = _strip_fences(tp).strip() + "\n"
    return out


# ---- phase 13/15: schema design -------------------------------------------
async def design_schema(brief: str, tech_plan: str) -> Dict[str, str]:
    """Ask the model for the SQL migration body + a one-line description. Returns
    {"sql": <migration sql>, "summary": <str>}. The SQL must be idempotent + parameterizable."""
    sys = ("You are a senior Postgres engineer. You write idempotent migrations only.\n\n"
           + NO_SAAS_RULE)
    user = (
        f"App brief: {brief}\n\nTechnical plan:\n{tech_plan[:6000]}\n\n"
        "Write the FIRST feature migration SQL (the app data tables — the template already has "
        "an _init migration and the app's own schema is `public`). Requirements:\n"
        "- Every statement idempotent: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS.\n"
        "- timestamptz for timestamps, sensible defaults (now()).\n"
        "- No reserved-keyword column names (use created_at, left_at, etc.).\n"
        "- Primary keys explicit (bigserial or uuid).\n"
        "Respond as JSON: {\"summary\":\"one line\",\"sql\":\"<the full migration SQL>\"}."
    )
    reply = await gpu.chat(
        [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        schema={"type": "object"}, temperature=0.2, num_predict=2000, timeout=300,
    )
    data = _extract_json(reply)
    sql = (data.get("sql") or "").strip()
    if not sql or "create table" not in sql.lower():
        raise ValueError("schema design returned no CREATE TABLE")
    return {"sql": sql, "summary": (data.get("summary") or "app data schema").strip()}


# ---- phase 15/18: per-feature file generation -----------------------------
# We ask the model to return a JSON map {path: full_file_contents}. Because JSON string
# escaping of large source files is where truncation bites, we scale the token budget to the
# combined size of the files it's likely to emit and REJECT any file that looks truncated.

_MIN_FILE_BUDGET = 6000
_TOKENS_PER_CHAR = 0.5   # generous; JSON-escaped source is dense


def _looks_truncated(path: str, content: str) -> bool:
    """Heuristic truncation check — the Kimi-bug guard. A file whose braces/tags are wildly
    unbalanced, or that ends mid-token, is treated as truncated and discarded."""
    if not content or not content.strip():
        return True
    c = content
    # balanced-ish braces/parens for code files
    if path.endswith((".js", ".ts", ".json", ".css")):
        if c.count("{") - c.count("}") not in range(-1, 3):
            return True
        if c.count("(") - c.count(")") not in range(-2, 3):
            return True
    if path.endswith((".html", ".htm")):
        low = c.lower()
        if "<html" in low and "</html>" not in low:
            return True
        if "<body" in low and "</body>" not in low:
            return True
    # a source file that ends on an obviously-open construct
    if c.rstrip().endswith(("{", "(", ",", "=>", "const", "return", "function")):
        return True
    if path.endswith(".json"):
        try:
            json.loads(c)
        except Exception:
            return True
    return False


async def generate_files(
    *,
    brief: str,
    tech_plan: str,
    feature: str,
    current_files: Dict[str, str],
    recent_changes: List[str],
    target_hint: str = "",
    minimal: bool = False,
) -> Dict[str, str]:
    """Generate/patch the file set for ONE feature. Returns {path: full_contents}.

    `current_files` is a SHORT context block (path -> current contents) of the files the model
    is allowed to see/edit — keep it small (the designer lesson). `recent_changes` is the last N
    change summaries. Full-file output only; truncated files are retried with more tokens.
    """
    sys = (
        "You are a meticulous full-stack engineer building one small feature at a time in an "
        "existing Node(Express)+Postgres+Redis app. You output COMPLETE files only — never a "
        "diff, never a fragment, never '...'. The app entry is server.js; frontend is "
        "public/index.html (self-contained); migrations live in migrations/NNN_*.sql and run on "
        "boot; the pg pool uses process.env.DATABASE_URL; redis uses process.env.REDIS_URL.\n\n"
        + NO_SAAS_RULE
    )
    ctx_files = "\n\n".join(
        f"----- FILE {p} -----\n{c[:8000]}" for p, c in current_files.items()
    ) or "(no files provided; you are creating them)"
    changes = "\n".join(f"- {c}" for c in recent_changes[-8:]) or "(none yet)"
    scope = ("Make the SMALLEST change that satisfies the request; touch the fewest files."
             if minimal else
             "Implement exactly this one backlog feature — nothing more, nothing less.")
    user = (
        f"App brief: {brief}\n\n"
        f"Technical plan (reference):\n{tech_plan[:5000]}\n\n"
        f"Recent changes:\n{changes}\n\n"
        f"Current relevant files:\n{ctx_files}\n\n"
        + (f"Edit target: {target_hint}\n\n" if target_hint else "")
        + f"FEATURE TO BUILD NOW: {feature}\n\n{scope}\n"
        "Return ONLY a JSON object mapping file path to that file's FULL new contents, e.g. "
        '{"server.js":"<entire file>","public/index.html":"<entire file>",'
        '"migrations/002_x.sql":"<entire file>"}. Only include files you actually change/create. '
        "Every included file must be complete and runnable. No commentary outside the JSON."
    )

    # scale the token budget to the size of what it's rewriting (truncation guard)
    seed_chars = sum(len(c) for c in current_files.values()) or 4000
    budget = max(_MIN_FILE_BUDGET, int(seed_chars * _TOKENS_PER_CHAR) + 4000)
    budget = min(budget, 30000)

    last_err: Optional[Exception] = None
    for attempt in range(3):
        reply = await gpu.chat(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            schema={"type": "object"}, temperature=0.25,
            num_predict=budget, timeout=420,
        )
        try:
            data = _extract_json(reply)
        except Exception as e:  # noqa: BLE001
            last_err = e
            budget = min(int(budget * 1.6), 30000)
            continue
        if not isinstance(data, dict) or not data:
            last_err = ValueError("file map empty")
            budget = min(int(budget * 1.6), 30000)
            continue
        files: Dict[str, str] = {}
        truncated: List[str] = []
        for path, content in data.items():
            if not isinstance(path, str) or not isinstance(content, str):
                continue
            path = path.lstrip("/").strip()
            if not path or ".." in path:
                continue
            content = _strip_fences(content) if content.strip().startswith("```") else content
            if _looks_truncated(path, content):
                truncated.append(path)
                continue
            files[path] = content
        if truncated:
            # discard the whole batch and retry bigger (never write a half-file)
            last_err = ValueError(f"truncated files discarded: {truncated}")
            logger.warning("codegen truncation on %s; retry with bigger budget", truncated)
            budget = min(int(budget * 1.8), 30000)
            continue
        if files:
            return files
        last_err = ValueError("no usable files after filtering")
        budget = min(int(budget * 1.6), 30000)
    raise RuntimeError(f"generate_files failed for '{feature}': {last_err}")
