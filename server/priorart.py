"""Phase 35 — the PRIOR-ART SCOUT: "is someone already building this?"

    "Build an online SimCity" is months of work from scratch. `amilich/isometric-city`
    already exists. The right answer to that sentence is not a 23-step build of a bad city
    simulator — it is *"we could start from this and add your features. Want to?"*

This module is the control-plane half. The work splits in three, and the split is the whole
design:

  1. **`classify()` — one cheap LLM call on the opening turn.** Could our 12-14 step pipeline
     produce a *good* version of this from nothing? A CRUD app: yes, build fresh, say nothing.
     A simulation, a game, an engine, a rich editor: no — go and look. It also writes the
     GitHub search queries, because "what would this be called on GitHub" is judgement and it
     is free to ask for in the same call.

  2. **`scout()` — the container.** `assistant-runtime/scout.py`, `docker run --rm`, capped,
     no Docker socket, **no credential of any kind**. It searches, clones and measures. It
     holds no model key and makes no judgement calls; everything it returns is a measurement.

  3. **`propose()` — one more LLM call, only if a candidate survived.** Turns the measurements
     into the user's own terms: what it already does, what we would add, what it costs them.

**The bar for speaking is deliberately high.** A silent miss costs nothing — the user gets
the build they asked for. A confident bad suggestion costs trust, and it costs it at the
exact moment the user is deciding whether this platform knows what it is doing. So: nothing
is proposed unless it is `adopt` or `adopt-with-work`, and the licence gate rejects before
anything else is even weighed.

**The licence gate is not a score, it is a gate.** Permissive only — MIT / Apache-2.0 / BSD /
ISC / Unlicense. Copyleft is rejected with a plain reason, because an AGPL base would quietly
make the user's own app AGPL forever, and they would find out when they tried to sell it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from server import gpu, usage
from server.db import pool

logger = logging.getLogger(__name__)

IMAGE = os.environ.get("ASSISTANT_IMAGE", "mikeos-assistant-runtime:latest")
NETWORK = os.environ.get("DEPLOY_NETWORK", "deploy_default")
RUN_UID = int(os.environ.get("ASSISTANT_UID", "10001"))

# The scout's outer wall. The container enforces its own inner deadline (SCOUT_DEADLINE_SEC)
# and always emits a document; this is what happens when it does not come back at all.
SCOUT_TIMEOUT_SEC = float(os.environ.get("SCOUT_TIMEOUT_SEC", "300"))
SCOUT_DEADLINE_SEC = float(os.environ.get("SCOUT_DEADLINE_SEC", "210"))
SCOUT_MEM = os.environ.get("SCOUT_MEM", "1g")
SCOUT_CPUS = os.environ.get("SCOUT_CPUS", "2")
SCOUT_PIDS = os.environ.get("SCOUT_PIDS", "256")
SCOUT_TMPFS = os.environ.get("SCOUT_TMPFS", "1500m")
ENABLED = os.environ.get("PRIOR_ART_ENABLED", "1").strip() not in ("0", "false", "no")

# ONE scout container at a time across the box, for the same reason there is one beat slot:
# background curiosity must never compete with a customer watching their build.
_slot = asyncio.Semaphore(1)

_FENCE = re.compile(r"<<<SCOUT_JSON>>>\s*(.*?)\s*<<<END_SCOUT_JSON>>>", re.S)

PERMISSIVE = {"MIT", "APACHE-2.0", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "ISC", "UNLICENSE",
              "0BSD", "CC0-1.0", "MIT-0", "BSD-4-CLAUSE", "ZLIB", "BSL-1.0"}


# ---------------------------------------------------------------------------
# 1. the classifier
# ---------------------------------------------------------------------------
_CLASSIFY_SYSTEM = """You screen one sentence — what somebody wants built — for an automated \
app factory, and you answer ONE question:

  **Could a 12-14 step automated pipeline write a GOOD version of this from nothing?**

The pipeline is real and its limits are known. It writes a Node + Express + Postgres + Redis \
web app: a schema, some CRUD routes, a handful of pages, auth, a dashboard. It is genuinely \
good at record-keeping software — trackers, directories, booking, invoicing, forums, \
inventories, small marketplaces, internal tools. Each of its ~13 steps is one focused code \
change; there is no room in that budget for a physics loop, a tile engine, a scheduler, a \
rules system, a parser, a rich text or graphics editor, or a simulation of anything.

Answer `false` (build it fresh, do not go looking) when the app IS mostly forms, lists, \
records and permissions — even a big one. That is what the pipeline does well, and a \
suggestion to start from somebody else's repo would be noise.

Answer `true` (go and look for prior art) when a good version needs an ENGINE: a game, a \
simulation, a map/tile/isometric world, a physics or economy model, an emulator, a graphics \
or audio or video editor, a diagramming or CAD surface, an IDE, a compiler or interpreter, a \
3D viewer, a real-time multiplayer world, a search engine, a spreadsheet — anything where \
the hard part is an algorithm rather than a table.

Also answer `true` when the thing they named is a WELL-TRODDEN piece of open-source \
infrastructure people usually self-host rather than rewrite (a wiki, a forum, a git host, a \
chat server, an analytics collector) AND the sentence does not describe something bespoke to \
them.

If it is genuinely borderline, answer `false`. A silent miss costs nothing; a confident bad \
suggestion costs trust.

When (and only when) you answer `true`, also write 3-5 GITHUB SEARCH QUERIES that would find \
existing open-source projects of that kind.

**GitHub's search ANDs every word you give it**, and it matches a repository's NAME, its \
one-line DESCRIPTION and its README — not what the project is really about. So each extra \
word is another way to miss the best repo in the world. Write them the way an experienced \
developer searches GitHub:
  * **Your FIRST query must be the broadest one: TWO OR THREE WORDS, no qualifiers at all.** \
Just the plain noun phrase for the thing, in the words its own author would have used in a \
one-line description. This is the query that finds the well-known project, and it is the one \
that most often gets ruined by helpfulness.
  * Then 1-3 NARROWER ones, which may add qualifiers where they genuinely help: `stars:>50`, \
`language:JavaScript`, `language:TypeScript`, `pushed:>2023-01-01`, `topic:<topic>`.
  * NEVER use filler that no repository description contains: "open source" (every repo on \
GitHub is), "clone", "project", "app", "web", "online", "best", "modern".
  * Be careful with `language:` — it matches the repo's ONE dominant language, so \
`language:JavaScript` silently excludes every TypeScript project. Put it on at most one query.
  * Do NOT include the user's own product name, their city, their language or any other \
detail personal to them — those words appear in no repository on earth.
  * Use SYNONYMS across the set rather than repeating one phrasing with different \
qualifiers: "city builder", "city simulation" and "citybuilder" find different repositories.

Reply with ONE JSON object and nothing else:

{"scout": true, "category": "isometric city-building simulation game",
 "reason": "one sentence, plain, on why the pipeline could or could not do this from zero",
 "queries": ["city building simulation", "isometric city builder stars:>50", "topic:citybuilder", "city simulation game language:TypeScript"]}"""


_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "scout": {"type": "boolean"},
        "category": {"type": "string"},
        "reason": {"type": "string"},
        "queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["scout", "category", "reason", "queries"],
}


def _extract_json(text: str) -> Any:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        return json.loads(t[i:j + 1])
    raise ValueError("no parseable JSON")


async def classify(seed: str, canvas_hint: str = "") -> dict:
    """{scout, category, reason, queries, cost_usd}. Never raises — a classifier that 500s
    would take the opening turn with it, and the opening turn is the product."""
    out = {"scout": False, "category": "", "reason": "", "queries": [], "cost_usd": 0.0}
    text = (seed or "").strip()
    if not text:
        return out
    body = f'They typed: "{text[:1200]}"'
    if canvas_hint:
        body += f"\n\nWhat the discussion has settled so far:\n{canvas_hint[:1200]}"
    try:
        with usage.capture() as recs:
            raw = await gpu.chat(
                [{"role": "system", "content": _CLASSIFY_SYSTEM},
                 {"role": "user", "content": body}],
                schema=_CLASSIFY_SCHEMA, temperature=0.2, num_predict=500,
                timeout=90, max_retries=2)
        out["cost_usd"] = sum(float(r.get("cost_usd") or 0) for r in recs)
        data = _extract_json(raw)
    except Exception as e:  # noqa: BLE001
        logger.info("prior-art classifier failed for %r: %s", text[:60], e)
        return out
    out["scout"] = bool(data.get("scout"))
    out["category"] = str(data.get("category") or "")[:200]
    out["reason"] = str(data.get("reason") or "")[:400]
    qs, seen = [], set()
    for q in (data.get("queries") or [])[:5]:
        s = re.sub(r"\s+", " ", str(q)).strip()[:180]
        if s and s.lower() not in seen:
            seen.add(s.lower())
            qs.append(s)
    out["queries"] = qs
    if out["scout"] and not qs:
        # A "go and look" with nothing to look for is a container spent on nothing.
        out["scout"] = False
        out["reason"] = (out["reason"] + " (no usable search queries)").strip()
    return out


# ---------------------------------------------------------------------------
# 2. the container
# ---------------------------------------------------------------------------
async def _docker(cmd: List[str], timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if err:
        logger.info("scout stderr tail: %s", err.decode("utf-8", "replace")[-1500:])
    return proc.returncode, (out or b"").decode("utf-8", "replace")


async def scout(queries: List[str], *, tag: str = "") -> dict:
    """Run ONE scout container to completion and return its document.

    Everything the safety story rests on is in the argument list below, and none of it is
    optional: `--rm`, `--cap-drop ALL`, `no-new-privileges`, a non-root uid, a read-only
    rootfs whose only writable surface is a tmpfs, hard memory/cpu/pids caps, **no Docker
    socket** and **no environment variable that is a credential**. The container clones
    arbitrary code from the internet; it must be able to do nothing else.
    """
    from server import assistant_runtime as AR          # reuse the image build/self-heal
    await AR.ensure_image()
    name = f"scout-{tag or 'x'}-{int(time.time())}"
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "--network", NETWORK,
        "--entrypoint", "python3",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", f"{RUN_UID}:{RUN_UID}",
        "--memory", SCOUT_MEM, "--memory-swap", SCOUT_MEM,
        "--cpus", str(SCOUT_CPUS), "--pids-limit", str(SCOUT_PIDS),
        "--read-only", "--tmpfs", f"/tmp:rw,size={SCOUT_TMPFS},mode=1777",
        "-e", "HOME=/tmp",
        "-e", "GIT_TERMINAL_PROMPT=0",
        "-e", "npm_config_cache=/tmp/.npm",
        "-e", f"SCOUT_QUERIES={json.dumps(queries)}",
        "-e", f"SCOUT_DEADLINE_SEC={SCOUT_DEADLINE_SEC}",
        IMAGE, "/app/scout.py",
    ]
    t0 = time.monotonic()
    async with _slot:
        try:
            rc, out = await _docker(cmd, timeout=SCOUT_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            await _docker(["docker", "rm", "-f", name], timeout=60)
            return {"ok": False, "error": f"scout exceeded {SCOUT_TIMEOUT_SEC}s",
                    "candidates": [], "seconds": round(time.monotonic() - t0, 1)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300], "candidates": [],
                    "seconds": round(time.monotonic() - t0, 1)}
    m = _FENCE.search(out)
    if not m:
        return {"ok": False, "error": f"scout produced no document (exit {rc})",
                "candidates": [], "seconds": round(time.monotonic() - t0, 1)}
    try:
        doc = json.loads(m.group(1))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"unparseable scout document: {e}",
                "candidates": [], "seconds": round(time.monotonic() - t0, 1)}
    doc["wall_seconds"] = round(time.monotonic() - t0, 1)
    return doc


# ---------------------------------------------------------------------------
# the gate — belt AND braces
# ---------------------------------------------------------------------------
def gate(candidates: List[dict]) -> List[dict]:
    """Re-apply the licence gate HERE, on the control plane, over the container's verdicts.

    The container already refuses copyleft. This is not redundant: the container is the thing
    running next to untrusted code, and a decision that would put a copyleft licence on a
    user's product must not depend on a process that a stranger's repository was unpacked
    inside. Two independent checks, and the one nearer the user is the one that counts.
    """
    out = []
    for c in candidates or []:
        lic = ((c.get("licence") or {}).get("spdx") or "").upper().replace("_", "-")
        if lic not in PERMISSIVE and lic.rstrip("+") not in PERMISSIVE:
            c = dict(c)
            if c.get("verdict") != "reject":
                logger.warning("prior-art: overriding %s verdict for %s — licence %r",
                               c.get("verdict"), c.get("full_name"), lic)
                c["verdict"] = "reject"
                c["blocking"] = "licence"
                c["why"] = (f"{lic or 'no licence'} is not a permissive licence, so it is "
                            "not something we will start your app from.")
        out.append(c)
    return out


def best(candidates: List[dict]) -> Optional[dict]:
    ok = [c for c in candidates or []
          if c.get("verdict") in ("adopt", "adopt-with-work")]
    if not ok:
        return None
    return sorted(ok, key=lambda c: -int(c.get("score") or 0))[0]


# ---------------------------------------------------------------------------
# 3. the proposal
# ---------------------------------------------------------------------------
_PROPOSE_SYSTEM = """You are the product lead in a pre-build discussion. You just found an \
existing open-source project that could be the STARTING POINT for what this person asked \
for, instead of building it from nothing.

Make the offer, honestly, in their terms. You are not selling: you are handing them a \
decision they are qualified to make.

What the offer must contain:
* **what it already is** — in their words, not the repo's. "It is a working isometric city \
grid with zoning and traffic" beats "a TypeScript canvas renderer".
* **what we would add** — their features, named, on top of it.
* **what it costs them** — the honest trade. Someone else's code and someone else's \
decisions; the licence they inherit; the size, if it is large; anything the evidence says is \
weak (dormant, big, an odd data layer). If the verdict was `adopt-with-work`, SAY what the \
work is.
* **that declining is fine** — one clause, no pressure. Building it fresh remains an option \
and the conversation carries on either way.

Rules you may not break:
* Every factual claim about the project must come from the EVIDENCE you are given. Do not \
guess at features, do not describe screens you were not told about, do not estimate a star \
count or a date. If the evidence does not say it, do not say it.
* Do not promise it will work. We deploy the unmodified project FIRST and find out; say so \
in one clause, because it is the reassuring part and it is true.
* No hype, no emoji, no bullet-point avalanche. Three or four short paragraphs at most.

Reply with ONE JSON object and nothing else:

{"headline": "one line: what we would start from, e.g. 'Start from isometric-city (MIT)'",
 "already": "what it already does, 1-2 sentences, in their terms",
 "we_add": "what we would build on top — their features, named",
 "cost": "the honest trade-off, 1-2 sentences",
 "reply": "the whole offer as you would say it in the room, markdown, 3-4 short paragraphs"}"""


def evidence_block(c: dict) -> str:
    """What the proposal writer is allowed to know. Deliberately the measurements and nothing
    else — the model cannot embellish what it was never told."""
    ev = c.get("evidence") or {}
    langs = ", ".join(
        "{} {}".format(l.get("lang"), l.get("loc")) for l in (ev.get("languages") or []))
    lines = [
        f"repository: {c.get('full_name')} ({c.get('url')})",
        f"its own description: {c.get('description') or '(none)'}",
        f"licence: {(c.get('licence') or {}).get('spdx')} "
        f"(found in {(c.get('licence') or {}).get('source')})",
        f"verdict: {c.get('verdict')} (score {c.get('score')})",
        f"stars: {c.get('stars')}, open issues: {c.get('open_issues')}, "
        f"archived: {c.get('archived')}",
        f"size: ~{ev.get('loc')} lines of code across {ev.get('files')} files",
        f"languages: {langs}",
        f"last commit: {ev.get('last_commit')} — {ev.get('last_commit_subject')}",
        f"commits in the last year: {ev.get('commits_last_year')}",
        f"has its own server: {ev.get('has_server')} {ev.get('server_files')}",
        f"static front-end only: {ev.get('static_only')} (roots {ev.get('static_roots')})",
        f"build step: {ev.get('build_step')} ({ev.get('bundler')})",
        f"data layer detected: {', '.join(ev.get('data_layer') or [])}",
        f"dependencies: {ev.get('dependencies')} direct — {ev.get('top_dependencies')}",
        f"ships a Dockerfile: {ev.get('has_dockerfile')}; compose: {ev.get('has_compose')}",
        f"dependency install check: {c.get('install')}",
        "what the scoring actually noted:",
    ]
    for n in (c.get("notes") or []):
        lines.append(f"  - {n}")
    readme = (ev.get("readme_head") or "").strip()
    if readme:
        lines.append("\nthe top of its README (verbatim):\n" + readme[:1500])
    return "\n".join(lines)


async def propose(seed: str, canvas_hint: str, cand: dict) -> dict:
    """{headline, already, we_add, cost, reply, cost_usd}. Degrades to a plain, factual
    offer assembled from the evidence if the model is unavailable — an offer written from
    measurements is worse prose and exactly as true."""
    out = {"headline": "", "already": "", "we_add": "", "cost": "", "reply": "",
           "cost_usd": 0.0}
    user = (f'What they asked for: "{(seed or "")[:800]}"\n\n'
            + (f"What the discussion has settled so far:\n{canvas_hint[:1500]}\n\n"
               if canvas_hint else "")
            + "THE EVIDENCE the scout measured (this is all you know about it):\n"
            + evidence_block(cand))
    try:
        with usage.capture() as recs:
            raw = await gpu.chat(
                [{"role": "system", "content": _PROPOSE_SYSTEM},
                 {"role": "user", "content": user}],
                schema={"type": "object"}, temperature=0.5, num_predict=1200,
                timeout=120, max_retries=2)
        out["cost_usd"] = sum(float(r.get("cost_usd") or 0) for r in recs)
        data = _extract_json(raw)
    except Exception as e:  # noqa: BLE001
        logger.info("prior-art proposal fell back to the evidence: %s", e)
        data = {}
    out["headline"] = str(data.get("headline") or "")[:200]
    out["already"] = str(data.get("already") or "")[:800]
    out["we_add"] = str(data.get("we_add") or "")[:800]
    out["cost"] = str(data.get("cost") or "")[:800]
    out["reply"] = str(data.get("reply") or "")[:4000]
    if not out["reply"]:
        lic = (cand.get("licence") or {}).get("spdx")
        out["headline"] = out["headline"] or f"Start from {cand.get('full_name')} ({lic})"
        out["reply"] = (
            f"Before we build this from scratch — **{cand.get('full_name')}** already exists "
            f"and looks like a usable starting point: {cand.get('headline')}.\n\n"
            f"{cand.get('description') or ''}\n\n"
            "We would import it into your own repo (keeping its licence and crediting the "
            "source), deploy it unchanged first to prove it runs, and then build your "
            "features on top of it. If you would rather we started from nothing, say so and "
            "we carry on as we were.")
    return out


# ---------------------------------------------------------------------------
# storage — one jsonb column on the discussion
# ---------------------------------------------------------------------------
async def save(discussion_id: str, prior_art: dict) -> None:
    await pool().execute(
        "UPDATE builderapps.discussions SET prior_art=$2::jsonb WHERE id=$1",
        discussion_id, json.dumps(prior_art))


async def load(discussion_id: str) -> dict:
    raw = await pool().fetchval(
        "SELECT prior_art FROM builderapps.discussions WHERE id=$1", discussion_id)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    return raw or {}


def summary(pa: dict) -> dict:
    """What the SPA is given. The full evidence for every candidate would be a lot of JSON on
    every poll, and the rejected ones matter only as a short, honest "we also looked at"."""
    pa = pa or {}
    cands = pa.get("candidates") or []
    return {
        "status": pa.get("status") or "",
        "category": pa.get("category") or "",
        "reason": pa.get("reason") or "",
        "queries": pa.get("queries") or [],
        "pick": pa.get("pick") or "",
        "proposal": pa.get("proposal") or {},
        "candidate": pa.get("candidate") or {},
        "also_considered": [
            {"full_name": c.get("full_name"), "url": c.get("url"),
             "verdict": c.get("verdict"), "headline": c.get("headline"),
             "licence": (c.get("licence") or {}).get("spdx") or "",
             "blocking": c.get("blocking") or "", "why": c.get("why") or ""}
            for c in cands if c.get("full_name") != (pa.get("pick") or "")][:6],
        "cost_usd": round(float(pa.get("cost_usd") or 0), 4),
        "seconds": pa.get("seconds") or 0,
        "considered": pa.get("considered") or 0,
        "decided_at": pa.get("decided_at") or 0,
        "error": pa.get("error") or "",
    }


def slim(cand: dict) -> dict:
    """The chosen candidate as it is stored and shown: the evidence a human can judge, not
    the 1,800-character README the model was given."""
    ev = dict(cand.get("evidence") or {})
    ev.pop("readme_head", None)
    out = dict(cand)
    out["evidence"] = ev
    return out


# ---------------------------------------------------------------------------
# the whole thing, as one background job
# ---------------------------------------------------------------------------
async def run_for_discussion(discussion_id: str, seed: str, canvas_hint: str = "") -> dict:
    """classify -> (maybe) scout -> gate -> (maybe) propose, writing progress as it goes.

    Runs as a background task off the OPENING TURN. It must never raise into its caller and
    it must always leave a terminal `status`, because the SPA polls this column and a job that
    dies silently leaves a spinner turning forever.
    """
    t0 = time.monotonic()
    pa: Dict[str, Any] = {"status": "classifying", "started_at": int(time.time() * 1000),
                          "cost_usd": 0.0}
    try:
        await save(discussion_id, pa)
        cls = await classify(seed, canvas_hint)
        pa["cost_usd"] = round(float(pa["cost_usd"]) + float(cls.get("cost_usd") or 0), 6)
        pa.update({"category": cls.get("category"), "reason": cls.get("reason"),
                   "queries": cls.get("queries") or []})
        if not cls.get("scout"):
            # THE COMMON CASE, and it is a success: the pipeline can build this well, so we
            # say nothing at all. `skipped` is terminal and the UI shows nothing for it.
            pa["status"] = "skipped"
            pa["seconds"] = round(time.monotonic() - t0, 1)
            await save(discussion_id, pa)
            logger.info("prior-art %s: not scouting — %s", discussion_id, cls.get("reason"))
            return pa

        pa["status"] = "scouting"
        await save(discussion_id, pa)
        doc = await scout(cls["queries"], tag=discussion_id)
        pa["considered"] = int(doc.get("considered") or 0)
        cands = gate(doc.get("candidates") or [])
        pa["candidates"] = [slim(c) for c in cands]
        pa["error"] = str(doc.get("error") or "")
        pick = best(cands)
        if not pick:
            # Nothing good enough. Also a success — and deliberately SILENT: proposing a dead
            # 2016 repo is worse than proposing nothing.
            pa["status"] = "none"
            pa["seconds"] = round(time.monotonic() - t0, 1)
            await save(discussion_id, pa)
            logger.info("prior-art %s: %d candidates, none worth proposing",
                        discussion_id, len(cands))
            return pa

        prop = await propose(seed, canvas_hint, pick)
        pa["cost_usd"] = round(float(pa["cost_usd"]) + float(prop.get("cost_usd") or 0), 6)
        prop.pop("cost_usd", None)
        pa.update({"status": "proposed", "pick": pick.get("full_name"),
                   "candidate": slim(pick), "proposal": prop})
        pa["seconds"] = round(time.monotonic() - t0, 1)
        await save(discussion_id, pa)
        logger.info("prior-art %s: proposing %s (%s, score %s) after %.1fs / $%.4f",
                    discussion_id, pick.get("full_name"),
                    (pick.get("licence") or {}).get("spdx"), pick.get("score"),
                    pa["seconds"], pa["cost_usd"])
        return pa
    except Exception as e:  # noqa: BLE001 — a scout must never take the room down
        logger.exception("prior-art scout failed for %s", discussion_id)
        pa["status"] = "failed"
        pa["error"] = str(e)[:300]
        pa["seconds"] = round(time.monotonic() - t0, 1)
        try:
            await save(discussion_id, pa)
        except Exception:  # noqa: BLE001
            pass
        return pa
