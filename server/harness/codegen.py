"""LLM codegen helpers for the create pipeline (phase 13/15) — prose and schema only.

What lives here is the work that genuinely *is* "write me a document": the six strategy docs
and the first data-model migration. **Per-feature code generation moved out** to
`server.harness.agentic`, which drives a tool-using loop (read/grep/edit) instead of asking
for whole files back as one JSON blob — see HARNESS-TOOLS.md for why every failure this
platform hit traced back to that one decision.

What stays here:

* **`NO_SAAS_RULE`** — the hard architecture contract repeated into EVERY codegen system
  prompt (agentic loop included): fully self-hosted Node+Postgres+Redis, no third-party SaaS,
  plus the platform contracts that got learned the hard way — rule 6 (the `/health` shape the
  deploy gate reads), rule 7 (never rename an existing endpoint's response keys to fix a
  frontend bug), rule 8 (never hand-roll a migration runner) and rule 9 (stay embeddable in
  the builder's preview iframe — harden with CSP `frame-ancestors`, never `X-Frame-Options`).
* **`PLATFORM_CONTRACTS`** — rules 6 and 9 on their own, for the *assistants*' coding agent,
  which is a different runtime (a container, not this process) and so is handed them over the
  wire rather than importing this module.
* **`write_strategy_docs`** / **`design_schema`** — one focused `gpu.chat` call each. The
  generated migration is run through the same syntax gate the agent's edits face, so a
  non-idempotent or truncated first migration can never reach the boot path.

All model output is parsed defensively (strip code fences, tolerate prose around JSON).
"""
import json
import logging
from typing import Any, Dict, List

from server import gpu
from server.harness import syntax
from server.harness.backlog import MAX_FEATURES as _MAX_BACKLOG

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The PLATFORM CONTRACTS — the two things an app owes its host rather than its users.
#
# Both were learned the same way: a perfectly healthy app was reported as broken because it
# changed something the platform reads. They are defined once, here, and composed into
# NO_SAAS_RULE (rules 6 and 9) AND shipped to the assistants' coding agent (`PLATFORM_CONTRACTS`
# -> /api/assistant/context -> the Pi grounding), so there is exactly one wording to keep true.
# ---------------------------------------------------------------------------
HEALTH_CONTRACT = (
    "`GET /health` belongs to the hosting platform, NOT to the app. It MUST "
    "always exist and MUST respond with exactly this shape: "
    "{\"status\":\"ok\",\"db\":\"ok\",\"redis\":\"ok\"} (each value is \"ok\" only when that datastore "
    "actually answered; use \"down\" otherwise). Never rename it, never change those field names, and "
    "never drop it when you rewrite server.js. The deployment health-gate reads this exact shape — "
    "change it and a perfectly working app gets reported as FAILED and never finishes building."
)

# NOTE ON WORDING: the origin below is the REAL, literal origin of the builder — it is not a
# sample to be adapted, renamed or substituted (a previous rule used a made-up example value
# and the model dutifully copied the example into a real endpoint). It is spelled out as a
# requirement for that reason.
EMBED_CONTRACT = (
    "The builder shows this app to its owner INSIDE AN IFRAME on the page "
    "https://builderapps.osmike.com — so the app MUST stay embeddable by that one origin. "
    "Hardening the app is right and welcome, but it has to be done with the header that can "
    "express an exception:\n"
    "  * If you send a Content-Security-Policy, its `frame-ancestors` directive MUST allow the "
    "builder. Write it exactly like this, with this exact origin — it is the real builder "
    "origin, not a placeholder to replace with your own value:\n"
    "        frame-ancestors 'self' https://builderapps.osmike.com\n"
    "  * NEVER send `frame-ancestors 'none'`.\n"
    "  * NEVER send an `X-Frame-Options` header at all — not DENY, not SAMEORIGIN. That header "
    "cannot express \"allow this one other origin\" (its ALLOW-FROM form is dead in Chrome), and "
    "browsers enforce it IN ADDITION to the CSP, so it silently overrides the frame-ancestors "
    "allowance you just wrote. CSP `frame-ancestors` is the only correct clickjacking control "
    "here; a helmet-style default that emits X-Frame-Options must have that header turned off.\n"
    "Get this wrong and the owner's preview pane shows the browser's \"refused to connect\" "
    "error, and an app that is serving 200s and passing its health check looks completely dead."
)

# What an assistant's coding agent is told about the platform. Same words as rules 6 and 9,
# without the numbering that only makes sense inside NO_SAAS_RULE.
PLATFORM_CONTRACTS = (
    "## Platform contracts (the hosting platform's, not the app's)\n\n"
    "Breaking either of these makes a working app look broken to its owner. They are not "
    "style preferences and they are not negotiable.\n\n"
    "- **Health endpoint.** " + HEALTH_CONTRACT + "\n\n"
    "- **Stay embeddable.** " + EMBED_CONTRACT
)

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
    "6. PLATFORM CONTRACT — " + HEALTH_CONTRACT + "\n"
    "7. Client and server response shapes MUST match, and the SERVER's existing shape is the source "
    "of truth. When the frontend consumes one of your own JSON endpoints, first read what that "
    "endpoint actually returns, then unwrap that exact key before iterating (if it returns "
    "{\"widgets\":[...]}, read `data.widgets`). Never iterate the response object directly — "
    "`data.forEach(...)` on an object throws \"forEach is not a function\" and the list silently never "
    "renders. **Never rename or reshape an existing endpoint's response keys to fix a frontend bug** "
    "— changing the server key while updating the client just moves the mismatch and breaks every "
    "other caller. Fix the CLIENT to match the server unless explicitly asked to change the API.\n"
    "8. MIGRATIONS — the scaffold ALREADY has a working, idempotent migration runner: `db/migrate.js` "
    "applies every `migrations/*.sql` once, in filename order, tracking them in a `_migrations` table, "
    "and `server.js` calls it on boot. DO NOT write your own migration runner and DO NOT create a "
    "second tracking table (e.g. `schema_migrations`) — a hand-rolled one has already shipped broken "
    "(`ON CONFLICT (filename)` with no UNIQUE constraint), which crash-loops the app at boot and fails "
    "the whole build. To add schema, ONLY add a new numbered file like `migrations/002_feature.sql`. "
    "Create each table exactly once across all migrations (never both `notes` and `public.notes`), "
    "always `IF NOT EXISTS`, and never ALTER or re-shape the scaffold's existing `app_meta` table. Any "
    "`ON CONFLICT (col)` you write REQUIRES a matching UNIQUE/PRIMARY KEY constraint on that column.\n"
    "9. PLATFORM CONTRACT — " + EMBED_CONTRACT
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
    f"## Build Backlog — an ORDERED numbered list of AT MOST {_MAX_BACKLOG} concrete build "
    "tasks, each ONE small feature (e.g. `1. Notes table migration + pool wiring`, "
    "`2. POST /api/notes create endpoint`, `3. GET /api/notes list newest-first`, "
    "`4. Frontend: add-note form + list render`, `5. DELETE /api/notes/:id + remove-from-list`). "
    "Each item must be independently buildable and testable. Keep the app SMALL and correct; do "
    "not over-scope.\n"
    f"HARD RULE: the backlog IS the build — only these {_MAX_BACKLOG} tasks get built. So every "
    "page you listed under `## Pages` must have its own backlog item, and every capability the "
    "brief asks for must be reachable from the UI: an endpoint with no page is a feature the "
    "user cannot use. If it does not all fit, cut scope in the Data Model and the Routes — "
    "never in the Pages, and never leave the admin/editor screen for last."
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
    messages = [{"role": "system", "content": sys}, {"role": "user", "content": user}]
    last_err = ""
    for attempt in range(3):
        reply = await gpu.chat(messages, schema={"type": "object"}, temperature=0.2,
                               num_predict=2000, timeout=300)
        try:
            data = _extract_json(reply)
        except Exception as e:  # noqa: BLE001
            last_err = f"your reply was not parseable JSON ({e})"
        else:
            sql = (data.get("sql") or "").strip()
            if not sql or "create table" not in sql.lower():
                last_err = "the reply contained no CREATE TABLE statement"
            else:
                # Same gate the agent's own writes face: a truncated or non-idempotent
                # migration here would crash-loop the app on its very first boot.
                err = syntax.check_sql(sql)
                if err is None:
                    return {"sql": sql,
                            "summary": (data.get("summary") or "app data schema").strip()}
                last_err = f"the SQL was rejected by the syntax gate: {err}"
        logger.warning("schema design attempt %d rejected: %s", attempt + 1, last_err)
        messages += [{"role": "assistant", "content": reply[:4000]},
                     {"role": "user", "content": f"That is not acceptable — {last_err}. "
                                                 "Send the corrected JSON again."}]
    raise ValueError(f"schema design failed: {last_err}")
