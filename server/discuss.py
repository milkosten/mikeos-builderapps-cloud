"""Phase 34 — "Discuss": the pre-build discussion room.

A discussion is a conversation that produces a BRIEF. It is text only: no repo, no subnet,
no container, no deploy — which is why it costs cents and why it lives in its own table
(`builderapps.discussions`, migration 014) instead of pretending to be a project.

Three things happen here:

  * `turn()` — one exchange with Kimi. It returns prose, 3-5 QUESTIONS THAT CHANGE THE BUILD,
    and a proposal for the canvas. The opening turn is deliberately different from every
    later one: it must reflect the user's sentence back as a CONCRETE DRAFT, because
    reacting to a proposal is far easier than specifying one from nothing. The model may
    also CALL A TOOL to read a page or a PDF off the open internet (`server.webread`) when
    the user points at one — the model decides, rather than us sniffing URLs out of the text,
    because "build me something like https://x.com/y" and "here is an example I dislike" are
    the same string and different intentions.

  * `merge_canvas()` — the living brief, with the one rule that makes the canvas trustworthy:
    **a decision the user has actually made is never silently overwritten.** See the
    docstring there; it is the part most likely to be "simplified" into a bug.

  * `compose_brief()` — the canvas + the decisions turned into the text that becomes
    `projects.prompt`, i.e. exactly what `_s_strategy` reads when it writes VISION.md. This
    function is the entire payoff of the phase: it is the difference between the pipeline
    being TOLD who the app is for and the pipeline GUESSING.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any, Dict, List, Optional

from server import gpu, usage, webread
from server.db import pool

logger = logging.getLogger(__name__)

# The canvas' shape, in the order it is rendered and briefed. `list` fields accumulate.
FIELDS: Dict[str, str] = {
    "name": "str",
    "vision": "str",
    "audience": "str",
    "features": "list",
    "stack": "str",
    "out_of_scope": "list",
}
FIELD_LABELS = {
    "name": "Working name",
    "vision": "Vision",
    "audience": "Who it's for",
    "features": "Core features",
    "stack": "Stack / data",
    "out_of_scope": "Out of scope",
}

MAX_MESSAGES = 200          # a room, not an archive; the oldest turns fall out of the prompt
MAX_TEXT = 8000             # per message, on the way in
MAX_DRAFT = 24000           # the half-filled questionnaire, as JSON, on the way in
_ALPHABET = "abcdefghijkmnopqrstuvwxyz23456789"


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
def gen_id() -> str:
    return "d" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


async def alloc_id() -> str:
    for _ in range(20):
        did = gen_id()
        if not await pool().fetchval("SELECT 1 FROM builderapps.discussions WHERE id=$1", did):
            return did
    raise RuntimeError("could not allocate a free discussion id after 20 tries")


def _row(row) -> Optional[dict]:
    """asyncpg hands jsonb back as a string. Decode once, here, so no caller has to care."""
    if row is None:
        return None
    d = dict(row)
    for k in ("messages", "canvas", "draft_answers"):
        v = d.get(k)
        if isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except Exception:  # noqa: BLE001 — a corrupt cell must not 500 the room
                d[k] = [] if k == "messages" else {}
    d["cost_usd"] = float(d.get("cost_usd") or 0)
    return d


async def create(user_id: str, seed: str, title: str = "") -> dict:
    did = await alloc_id()
    row = await pool().fetchrow(
        "INSERT INTO builderapps.discussions (id,user_id,seed,title,messages,canvas) "
        "VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb) RETURNING *",
        did, user_id, seed[:MAX_TEXT], title[:120], json.dumps([]), json.dumps({}),
    )
    return _row(row)


async def get(discussion_id: str, user_id: Optional[str] = None) -> Optional[dict]:
    if user_id is not None:
        row = await pool().fetchrow(
            "SELECT * FROM builderapps.discussions WHERE id=$1 AND user_id=$2",
            discussion_id, user_id)
    else:
        row = await pool().fetchrow(
            "SELECT * FROM builderapps.discussions WHERE id=$1", discussion_id)
    return _row(row)


async def list_for(user_id: str, limit: int = 100) -> List[dict]:
    rows = await pool().fetch(
        "SELECT id,title,seed,status,project_id,cost_usd,turns,created_at,updated_at, "
        "       jsonb_array_length(messages) AS message_count "
        "FROM builderapps.discussions WHERE user_id=$1 "
        "ORDER BY updated_at DESC LIMIT $2",
        user_id, limit)
    out = []
    for r in rows:
        d = dict(r)
        d["cost_usd"] = float(d.get("cost_usd") or 0)
        out.append(d)
    return out


async def save(discussion_id: str, *, messages: List[dict], canvas: dict,
               title: str, cost_delta: float = 0.0, turn_delta: int = 0) -> Optional[dict]:
    row = await pool().fetchrow(
        "UPDATE builderapps.discussions SET messages=$2::jsonb, canvas=$3::jsonb, title=$4, "
        "  cost_usd=cost_usd+$5, turns=turns+$6, updated_at=now() "
        "WHERE id=$1 RETURNING *",
        discussion_id, json.dumps(messages[-MAX_MESSAGES:]), json.dumps(canvas),
        (title or "")[:120], cost_delta, turn_delta)
    return _row(row)


async def save_draft_answers(discussion_id: str, draft: dict) -> None:
    """Persist the HALF-FILLED questionnaire.

    This is a scratchpad, not a turn: it never touches `messages`, `canvas`, `turns` or
    `updated_at`, and it never reaches the model. It exists because the rest of this room
    restores from the server and a questionnaire three answers deep should not be the one
    thing a reload throws away. `updated_at` is deliberately left alone — typing an answer is
    not activity that should reorder the Apps list.

    The size cap is enforced by REJECTING an oversized draft in the router, never by
    truncating the JSON here — half a JSON document does not cast to jsonb, and a scratchpad
    write must not be able to 500 the room.
    """
    body = json.dumps(draft or {})
    if len(body) > MAX_DRAFT:
        raise ValueError("draft too large")
    await pool().execute(
        "UPDATE builderapps.discussions SET draft_answers=$2::jsonb WHERE id=$1",
        discussion_id, body)


async def link_project(discussion_id: str, project_id: str) -> None:
    """Bind the two, BOTH ways. The discussion keeps `project_id` (what came of it) and the
    project keeps `discussion_id` (why it is the way it is) — a build whose scope decisions
    are only recoverable from a conversation nobody can find from the app is no better than
    no record at all."""
    await pool().execute(
        "UPDATE builderapps.discussions SET status='built', project_id=$2, updated_at=now() "
        "WHERE id=$1", discussion_id, project_id)
    await pool().execute(
        "UPDATE builderapps.projects SET discussion_id=$2 WHERE id=$1",
        project_id, discussion_id)


async def delete(discussion_id: str, user_id: str) -> bool:
    res = await pool().execute(
        "DELETE FROM builderapps.discussions WHERE id=$1 AND user_id=$2",
        discussion_id, user_id)
    return res.endswith(" 1")


# ---------------------------------------------------------------------------
# the canvas
# ---------------------------------------------------------------------------
def _norm_list(v: Any) -> List[str]:
    if isinstance(v, str):
        v = [x for x in re.split(r"[\n;]|(?:,\s(?=[A-Z]))", v) if x.strip()]
    if not isinstance(v, list):
        return []
    out, seen = [], set()
    for item in v:
        s = str(item).strip().lstrip("-•* ").strip()
        if not s or len(s) > 240:
            s = s[:240]
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out[:20]


def _norm_str(v: Any) -> str:
    return str(v or "").strip()[:1200]


def field(canvas: dict, name: str) -> Any:
    cell = (canvas or {}).get(name) or {}
    if not isinstance(cell, dict):
        return cell
    return cell.get("value") if cell.get("value") is not None else ("" if FIELDS.get(name) == "str" else [])


def has_content(canvas: dict) -> bool:
    return any(field(canvas, f) for f in FIELDS)


def merge_canvas(canvas: dict, proposal: dict, decided: List[str],
                 revisions: List[dict]) -> dict:
    """Apply one turn's proposal to the canvas WITHOUT ever silently overwriting a decision.

    Every cell is `{value, agreed, source}`. `agreed` is set the moment the model reports
    that the user settled that field (`decided`) — i.e. it is a claim about the USER, not
    about the model's confidence in its own draft.

    The rule, and the reason the canvas can be trusted:

      * empty cell            -> fill it (source `draft`)
      * cell exists, NOT agreed -> replace it. It was only ever the model's own sketch, and
                                 a sketch that cannot be refined is just clutter.
      * cell exists AND agreed -> **the proposal is IGNORED.** The only way an agreed value
                                 changes is an explicit entry in `revisions`, which carries a
                                 reason and is written to `changelog` — so the change appears
                                 in the UI as "Audience: from X to Y, because you said Z"
                                 rather than happening behind the user's back.

    List fields accumulate (union, order preserved, case-insensitive dedupe) instead of being
    replaced, so a later turn that mentions two features cannot quietly drop the other three.
    A revision may replace a list wholesale — that is what an explicit revision is for.
    """
    canvas = dict(canvas or {})
    decided_set = {str(d).strip() for d in (decided or []) if str(d).strip() in FIELDS}
    changelog = list(canvas.get("changelog") or [])

    # 1) explicit revisions first: they are allowed to touch agreed cells, and they must be
    #    recorded before the ordinary merge can no-op them.
    for rev in (revisions or []):
        if not isinstance(rev, dict):
            continue
        name = str(rev.get("field") or "").strip()
        if name not in FIELDS:
            continue
        cell = canvas.get(name) or {}
        old = cell.get("value") if isinstance(cell, dict) else cell
        new = _norm_list(rev.get("to")) if FIELDS[name] == "list" else _norm_str(rev.get("to"))
        if not new or new == old:
            continue
        canvas[name] = {"value": new, "agreed": True, "source": "revision"}
        changelog.append({
            "field": name,
            "label": FIELD_LABELS.get(name, name),
            "from": old if isinstance(old, str) else ", ".join(old or []),
            "to": new if isinstance(new, str) else ", ".join(new),
            "because": _norm_str(rev.get("because"))[:300],
        })
        decided_set.discard(name)          # already applied, do not re-apply below

    # 2) the ordinary proposal.
    for name, kind in FIELDS.items():
        if name not in (proposal or {}):
            continue
        cell = canvas.get(name) if isinstance(canvas.get(name), dict) else {}
        cur = cell.get("value") if cell else None
        agreed = bool(cell.get("agreed")) if cell else False
        prop = _norm_list(proposal.get(name)) if kind == "list" else _norm_str(proposal.get(name))
        if not prop:
            continue
        if kind == "list":
            # Accumulate. An agreed list still grows — adding a feature is not overwriting a
            # decision — but nothing already in it can be removed except by a revision.
            merged = _norm_list(list(cur or []) + prop) if cur else prop
            if merged == (cur or []):
                if name in decided_set and cell:
                    cell["agreed"] = True
                    cell["source"] = "decision"
                continue
            canvas[name] = {"value": merged,
                            "agreed": agreed or name in decided_set,
                            "source": "decision" if (agreed or name in decided_set) else "draft"}
            continue
        if cur and agreed:
            continue                    # THE RULE: an agreed value is never quietly replaced
        canvas[name] = {"value": prop,
                        "agreed": name in decided_set,
                        "source": "decision" if name in decided_set else "draft"}

    # 3) a field the user settled without the model re-proposing its text: promote in place.
    for name in decided_set:
        cell = canvas.get(name)
        if isinstance(cell, dict) and cell.get("value"):
            cell["agreed"] = True
            cell["source"] = "decision"

    canvas["changelog"] = changelog[-25:]
    return canvas


# ---------------------------------------------------------------------------
# the model turn
# ---------------------------------------------------------------------------
_SYSTEM = """You are the founding product lead running a short PRE-BUILD discussion with \
someone who is about to have a real full-stack web app built and deployed for them by an \
automated pipeline (Node + Postgres + Redis, one app on its own URL).

Your job is to turn their sentence into a shared understanding BEFORE anything is built. \
The pipeline that follows you writes the vision, the ICP, the UX notes, the schema and the \
backlog from whatever brief you produce — so anything you fail to settle here will be \
INVENTED by a machine that has never met this person.

How you behave:

* NEVER open with "what would you like to build?". They already told you. Reflect it back \
as a CONCRETE DRAFT PROPOSAL — a working name, what it does, the two or three screens it \
needs, what you would deliberately leave out. Reacting to a proposal is far easier than \
specifying one, and a proposal they disagree with is more useful than a blank page.
* Ask 3-5 questions AND NO MORE, and only ones that CHANGE WHAT GETS BUILT: who it is for, \
where the scope stops, who owns the data / whether it needs accounts and logins, and the \
one feature whose absence would make it a failure. Never ask trivia (colours, fonts, \
"any other thoughts?"), never ask something they have already answered, and never re-ask a \
question that is already settled on the canvas.
* Give each question 2-4 concrete options as `options`, with your RECOMMENDATION named in \
`recommended` and the reason in one short clause. Options are a shortcut, not a cage: the \
user may always answer in their own words, may pick none of them, and may say "you decide" \
— in which case take your own recommendation, say so plainly, and move on. \
**DEFAULT TO `"multi": true`.** Most real questions here have several honest answers at once \
(who it is for, which features matter, what to leave out), and forcing one choice makes the \
user throw away information you need. Only set `"multi": false` for a genuinely EXCLUSIVE \
choice — one where picking two would contradict itself (e.g. "self-hosted accounts OR no \
accounts at all"). If you are unsure, use true.
* THE ANSWERS COME BACK AS ONE COMPLETE SET, question by question. The user works through \
your questions in a stepper and submits them together, so what you receive is ALL of it — \
there is no second instalment coming. Any question marked NOT ANSWERED / SKIPPED is exactly \
that: **the user did not answer it, and you do not know the answer.** You may not invent, \
infer or quietly assume one, you may not write it into the canvas, and you may NEVER list \
its field in `decided`. Say plainly which questions are still open, then either ask them \
again (rephrased, with better options) or state the assumption you intend to proceed on and \
label it as YOUR assumption. A skipped question turned into an agreed decision is the worst \
thing you can do in this room: agreed cells are locked, so a guess becomes permanent.
* Talk like a person: short paragraphs, plain words, no bullet-point avalanche, no \
corporate filler, no emoji. Markdown is fine (bold, lists, `##` headings).
* As things get settled, update the canvas. List a field in `decided` ONLY when the user has \
actually settled it in their own words or by picking an option — not when you merely \
proposed it, and never on the strength of a question they skipped. To CHANGE something \
already settled, use `revisions` with a reason; never overwrite an agreed decision silently. \
A revision's `to` is the COMPLETE NEW VALUE of that field (for a list, the whole new list), \
never a description of what you changed — "removed the demand graphs" is a note, not a value, \
and it ends up rendered to the user as if it were the feature.
* When they ask to see the vision / the plan / the brief, set `show` to "vision" (just the \
vision) or "canvas" (the whole brief) AND write it out in `reply` as readable prose — they \
asked to READ it, so an empty acknowledgement is a failure.
* You can READ THE WEB. When the user points you at a page or a PDF ("look at this", "the \
site does X", a pasted link they want you to study), call `read_web_page` and use what it \
actually says. Judgement, not reflex: a link mentioned in passing, or one they are telling \
you they DISLIKE, does not need fetching unless its content would change the build. Rules \
you may not break: only claim to have read something the tool actually returned; if it was \
refused or failed, SAY SO plainly in `reply` and carry on without it; and when a fact came \
from a page, name the source in your own words ("their pricing page lists three tiers") \
rather than presenting it as your own idea.
* They can press "Build it" at any moment, so every turn must leave the canvas in a state \
worth building. Do not stall for completeness.

Reply with ONE JSON object and nothing else:

{
  "reply": "your message, markdown",
  "questions": [{"q": "the question", "options": ["...","..."], "recommended": "...", \
"why": "why this changes the build", "multi": true}],
  "canvas": {"name": "...", "vision": "...", "audience": "...",
             "features": ["..."], "stack": "...", "out_of_scope": ["..."]},
  "decided": ["audience"],
  "revisions": [{"field": "audience", "to": "...", "because": "you said ..."}],
  "show": "",
  "ready": false
}

`canvas` holds your CURRENT best statement of each field (vision = 2-4 sentences; features = \
3-8 short items; stack = data + auth in one line). `questions` may be empty once everything \
that matters is settled — say so and offer to build. `ready` is true when the brief is good \
enough to build."""


def _render_canvas(canvas: dict) -> str:
    if not has_content(canvas):
        return "(empty — nothing has been settled yet)"
    lines = []
    for name in FIELDS:
        v = field(canvas, name)
        if not v:
            continue
        cell = canvas.get(name) or {}
        mark = " [AGREED — do not change without a revision]" if cell.get("agreed") else " [draft]"
        text = v if isinstance(v, str) else "; ".join(v)
        lines.append(f"- {FIELD_LABELS[name]}{mark}: {text}")
    return "\n".join(lines)


def _render_thread(messages: List[dict], limit: int = 24) -> List[Dict[str, str]]:
    """The conversation as chat messages, with each assistant turn's questions folded back in
    so the model can see what it asked and never asks it twice."""
    out: List[Dict[str, str]] = []
    for m in messages[-limit:]:
        role = "assistant" if m.get("role") == "assistant" else "user"
        text = str(m.get("text") or "")
        qs = m.get("questions") or []
        if role == "assistant" and qs:
            asked = "; ".join(str(q.get("q") or "") for q in qs if isinstance(q, dict))
            if asked:
                text += f"\n(questions asked: {asked})"
        if text.strip():
            out.append({"role": role, "content": text[:4000]})
    return out


def _extract_json(text: str) -> Any:
    """Best-effort: pull the first JSON object out of a model reply (same contract as
    codegen._extract_json — duplicated rather than imported so the discussion room does not
    drag the whole build harness into its import graph)."""
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
        try:
            return json.loads(t[i:j + 1])
        except Exception:  # noqa: BLE001
            pass
    raise ValueError("no parseable JSON in model reply")


# "show me the vision" must ALWAYS render it. The model is instructed to set `show`, but an
# instruction is a request and this is a promise — so the ask is also detected here, and the
# detection wins. A turn that answered the words but did not show the thing is the exact
# failure this phase exists to prevent.
_SHOW_VISION_RE = re.compile(
    r"\b(show|see|read|what'?s|display|give me|render)\b[^.?!]{0,40}\b(the\s+)?(vision|mission)\b",
    re.I)
_SHOW_CANVAS_RE = re.compile(
    r"\b(show|see|read|what'?s|display|give me|render)\b[^.?!]{0,40}"
    r"\b(the\s+)?(canvas|brief|plan|spec|summary|whole thing|everything)\b", re.I)


def wants_shown(text: str) -> str:
    t = text or ""
    if _SHOW_VISION_RE.search(t):
        return "vision"
    if _SHOW_CANVAS_RE.search(t):
        return "canvas"
    return ""


def _clean_questions(raw: Any) -> List[dict]:
    out: List[dict] = []
    for q in (raw or [])[:5]:
        if isinstance(q, str):
            q = {"q": q}
        if not isinstance(q, dict):
            continue
        text = _norm_str(q.get("q") or q.get("question"))[:300]
        if not text:
            continue
        # 140, not 80. The stepper shows ONE question at a time in an 800px measure, so an
        # option has room to be a sentence — and at 80 the model's own wording was being cut
        # mid-word ("…one postcode, one apartment b"), which reads as a rendering bug. The
        # cap on `recommended` must match, or a recommendation stops matching its own option.
        opts, seen = [], set()
        for o in (q.get("options") or [])[:4]:
            s = str(o).strip()[:140]
            if s and s.lower() not in seen:
                seen.add(s.lower())
                opts.append(s)
        out.append({"q": text, "options": opts,
                    "recommended": _norm_str(q.get("recommended"))[:140],
                    "why": _norm_str(q.get("why"))[:200],
                    # MULTI-SELECT UNLESS THE MODEL SAYS OTHERWISE. Most questions here have
                    # several honest answers at once (audience, which features matter, what to
                    # leave out); forcing one choice throws away information we asked for.
                    "multi": bool(q.get("multi", True))})
    return out


# ---------------------------------------------------------------------------
# the answer set
# ---------------------------------------------------------------------------
# The questionnaire is answered as ONE SET and arrives as one user turn. Everything below
# exists because of the bug this replaced: a single chip click was posted as a whole user
# message, the model saw four questions asked and one paragraph back, and it completed the
# pattern — inventing the other three answers and marking them DECIDED, which under the
# canvas rules locks them for good. An answer set is explicit about every question,
# including the ones with no answer, so "unanswered" is a fact the model is told rather than
# a gap it fills in.
def clean_answers(raw: Any) -> List[dict]:
    out: List[dict] = []
    for a in (raw or [])[:8]:
        if not isinstance(a, dict):
            continue
        q = _norm_str(a.get("q"))[:300]
        if not q:
            continue
        ans = _norm_str(a.get("answer"))[:1200]
        # AN EMPTY ANSWER IS A SKIP. There is no third state: an empty string posted as if it
        # were an answer is exactly the "silently treated as answered" failure, one level down.
        out.append({"q": q, "answer": ans, "skipped": bool(a.get("skipped")) or not ans})
    return out


def answers_text(answers: List[dict]) -> str:
    """The user's turn as it is STORED AND SHOWN — the readable record of what was asked and
    what was chosen. The thread has to stay a truthful transcript, so a skipped question
    appears here as a skipped question rather than quietly vanishing."""
    lines = ["Answers to your questions:"]
    for i, a in enumerate(answers or [], 1):
        lines.append(f"\n{i}. {a.get('q') or ''}")
        if a.get("skipped") or not (a.get("answer") or "").strip():
            lines.append("   (skipped — no answer)")
        else:
            lines.append("   " + str(a["answer"]).strip())
    return "\n".join(lines)


def _answers_prompt(answers: List[dict]) -> str:
    """The same set, framed for the model, with the rule attached to the data it applies to.
    The rule is repeated here and not only in the system prompt on purpose: it is the last
    thing the model reads before it answers, and it is the one it used to break."""
    lines = ["The user worked through your questions and submitted them together. THIS IS "
             "THE COMPLETE ANSWER SET — nothing else is coming.\n"]
    skipped = []
    for i, a in enumerate(answers or [], 1):
        lines.append(f"{i}. Q: {a.get('q') or ''}")
        if a.get("skipped") or not (a.get("answer") or "").strip():
            lines.append("   NOT ANSWERED — the user skipped this question.")
            skipped.append(str(a.get("q") or ""))
        else:
            lines.append(f"   ANSWER: {str(a['answer']).strip()}")
    lines.append("")
    if skipped:
        lines.append(
            f"{len(skipped)} question(s) were NOT ANSWERED. You do not know those answers. "
            "Do NOT invent, infer or assume them, do NOT write them into the canvas, and do "
            "NOT put their fields in `decided`. Name them in your reply as still open, and "
            "either ask them again or say plainly what you will assume and that it is your "
            "assumption, not their decision.")
    else:
        lines.append("Every question was answered — treat these, and only these, as the "
                     "user's decisions.")
    return "\n".join(lines)


# The one tool the room has. Described in terms of the DECISION to use it, not the mechanics
# — the model is choosing when a page matters, and the description is the only place that
# judgement can be stated.
_TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_web_page",
        "description": (
            "Open a PUBLIC web page or PDF and read its text. Use it when the user points at "
            "something whose actual contents would change what gets built — a product they "
            "want this to resemble, a spec, a price list, a document they pasted. Do not use "
            "it for a link merely mentioned in passing. Returns the readable text (bounded); "
            "internal MikeOS hosts, private networks and non-http schemes are refused."),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The full public URL to read."},
                "why": {"type": "string",
                        "description": "One short clause: what you expect it to settle."},
            },
            "required": ["url"],
        },
    },
}]
MAX_TOOL_CALLS = 3          # per turn. A discussion is not a research project.


def _tool_payload(res: dict) -> str:
    """What the model is told the fetch produced. A refusal has to read as a REFUSAL — an
    empty result dressed up as content is how a model ends up narrating a page it never saw."""
    if res.get("refused"):
        return ("REFUSED — this URL was blocked and NOT fetched. Tell the user plainly, in "
                f"your reply, that you could not open it and why: {res.get('error')}")
    if not res.get("ok"):
        return ("COULD NOT READ this URL — you have NOT seen its contents. Say so in your "
                f"reply and continue without it. Reason: {res.get('error')}")
    head = (f"Read {res.get('url')} — \"{res.get('title')}\" ({res.get('kind')}, "
            f"{res.get('words')} words"
            + (", TRUNCATED to the first part" if res.get("truncated") else "") + "):\n\n")
    return head + (res.get("text") or "")


async def _run_tools(reply: dict, convo: List[Dict[str, Any]], sources: List[dict],
                     budget: List[int]) -> None:
    """Execute one round of tool calls, appending the results to `convo` and the provenance
    to `sources`. `budget` is a one-element list so the cap survives across rounds."""
    convo.append(gpu.assistant_tool_message(reply))
    for tc in reply.get("tool_calls") or []:
        args = tc.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except Exception:  # noqa: BLE001 — a mangled argument object is the model's bug
                args = {}
        url = str((args or {}).get("url") or "").strip()
        if budget[0] <= 0:
            convo.append(gpu.tool_result_message(
                tc["id"], tc["name"],
                "REFUSED — you have already read the maximum number of pages for this turn."))
            continue
        budget[0] -= 1
        res = await webread.read(url)
        sources.append({
            "url": res.get("url") or url,
            "label": webread.short_host(res.get("url") or url),
            "title": res.get("title") or "",
            "kind": res.get("kind") or "",
            "words": int(res.get("words") or 0),
            "ok": bool(res.get("ok")),
            "refused": bool(res.get("refused")),
            "error": res.get("error") or "",
        })
        convo.append(gpu.tool_result_message(tc["id"], tc["name"], _tool_payload(res)))


_JSON_NUDGE = ("Now give your answer as the single JSON object described in your "
               "instructions — no prose outside it.")


async def turn(disc: dict, user_text: str = "", answers: Optional[List[dict]] = None) -> dict:
    """One exchange. Returns {reply, questions, canvas, show, ready, sources, cost_usd}.

    No assistant turn yet = the OPENING turn (react to the seed). Note that `messages` is NOT
    empty by then: the user's own sentence is the first message in the thread (it is what
    they asked for, and a room whose first bubble is an answer to an invisible question is
    unreadable on reload) — so "opening" is the absence of a REPLY, not the absence of
    messages.

    The cost is captured across the WHOLE turn — tool rounds included — rather than inferred,
    because "a discussion is cheap" is a claim this phase makes and a claim has to be
    measurable.
    """
    canvas = disc.get("canvas") or {}
    messages = list(disc.get("messages") or [])
    opening = not any(m.get("role") == "assistant" for m in messages)

    convo: List[Dict[str, Any]] = [{"role": "system", "content": _SYSTEM}]
    if opening:
        convo.append({"role": "user", "content":
            f"The person typed this on the start page and pressed Discuss:\n\n"
            f"\"{disc.get('seed') or ''}\"\n\n"
            "This is your OPENING turn. Do not ask them what they want to build — they just "
            "said. Propose a concrete draft of it (working name, what it does, the screens it "
            "needs, what you would leave out), then ask 3-5 questions that actually change "
            "what gets built. Fill the canvas with your draft."})
    else:
        convo.append({"role": "user", "content":
            f"Original one-line idea: \"{disc.get('seed') or ''}\"\n\n"
            f"The canvas as it stands:\n{_render_canvas(canvas)}\n\n"
            "What follows is the conversation so far. Continue it."})
        # The turn being answered is ALREADY the last message in `messages` (it is written
        # before the model is called, so a model failure cannot lose it). Render the history
        # WITHOUT it and then append it in its framed form, or the model reads the user's
        # words twice — once bare, once with the rules attached — and the bare copy wins.
        tail = messages[-1] if messages and messages[-1].get("role") == "user" else None
        convo.extend(_render_thread(messages[:-1] if tail else messages))
        if answers is None and tail:
            answers = tail.get("answers")
        if answers:
            convo.append({"role": "user", "content": _answers_prompt(answers)})
        else:
            convo.append({"role": "user",
                          "content": user_text or (tail or {}).get("text") or "(continue)"})

    sources: List[dict] = []
    with usage.capture() as recs:
        raw = ""
        budget = [MAX_TOOL_CALLS]
        for _round in range(MAX_TOOL_CALLS + 1):
            reply = await gpu.chat_tools(convo, _TOOLS, temperature=0.55, num_predict=2600,
                                         timeout=180, max_retries=3)
            raw = reply.get("content") or ""
            if not reply.get("tool_calls"):
                break
            await _run_tools(reply, convo, sources, budget)
        # Tool-calling and strict JSON output do not always co-operate: a model that has just
        # been handed a page sometimes answers in prose. One forced JSON pass fixes it, and it
        # only costs anything on the turns that actually need it.
        try:
            _extract_json(raw)
        except Exception:  # noqa: BLE001
            convo.append({"role": "assistant", "content": raw[:2000] or "(thinking)"})
            convo.append({"role": "user", "content": _JSON_NUDGE})
            raw = await gpu.chat(convo, schema={"type": "object"}, temperature=0.4,
                                 num_predict=2600, timeout=180, max_retries=2)
    cost = sum(float(r.get("cost_usd") or 0) for r in recs)

    try:
        data = _extract_json(raw)
        if not isinstance(data, dict):
            raise ValueError("reply was not an object")
    except Exception as e:  # noqa: BLE001
        # A room that dies on a malformed reply is worse than one that says so and keeps the
        # thread. NEVER lose the user's message to a parse error.
        logger.warning("discuss turn unparseable for %s: %s", disc.get("id"), e)
        return {"reply": "Sorry — I lost my train of thought there. Say that again?",
                "questions": [], "canvas": canvas, "show": "", "ready": False,
                "sources": sources, "cost_usd": cost, "error": True}

    reply = _norm_str(data.get("reply"))[:6000] or "…"
    questions = _clean_questions(data.get("questions"))
    decided = data.get("decided") if isinstance(data.get("decided"), list) else []
    revisions = data.get("revisions") if isinstance(data.get("revisions"), list) else []
    proposal = data.get("canvas") if isinstance(data.get("canvas"), dict) else {}
    canvas = merge_canvas(canvas, proposal, decided, revisions)

    show = str(data.get("show") or "").strip().lower()
    if show not in ("vision", "canvas"):
        show = ""
    forced = wants_shown(user_text)
    if forced:
        show = forced          # the promise beats the model's judgement

    return {"reply": reply, "questions": questions, "canvas": canvas, "show": show,
            "ready": bool(data.get("ready")), "sources": sources, "cost_usd": cost}


# ---------------------------------------------------------------------------
# the brief
# ---------------------------------------------------------------------------
def compose_brief(disc: dict) -> str:
    """The text that becomes `projects.prompt` — i.e. what `_s_strategy` actually reads.

    THIS IS THE POINT OF THE WHOLE PHASE. Today the pipeline receives one sentence and
    invents the brand, the audience and the scope from it. Here it receives the decisions,
    in the user's own terms, with the non-goals stated — so VISION.md describes the app that
    was discussed rather than a plausible app with a similar name.

    The transcript goes in AFTER the settled brief and is truncated, in that order on
    purpose: the canvas is what was AGREED and must survive any truncation, the conversation
    is context that is nice to have.
    """
    canvas = disc.get("canvas") or {}
    seed = (disc.get("seed") or "").strip()
    parts: List[str] = []

    name = field(canvas, "name")
    vision = field(canvas, "vision")
    audience = field(canvas, "audience")
    features = field(canvas, "features") or []
    stack = field(canvas, "stack")
    out_of_scope = field(canvas, "out_of_scope") or []

    head = f"{name}: {vision}" if name and vision else (vision or seed)
    parts.append(head)
    if seed and seed.lower() not in head.lower():
        parts.append(f"\nOriginal idea, in the owner's words: {seed}")
    if name:
        parts.append(f"\nProduct name (DECIDED — use this name, do not invent another): {name}")
    if audience:
        parts.append(f"\nWho it is for: {audience}")
    if features:
        parts.append("\nCore features (these are the agreed scope):\n"
                     + "\n".join(f"- {f}" for f in features))
    if stack:
        parts.append(f"\nData, accounts and stack: {stack}")
    if out_of_scope:
        parts.append("\nExplicitly OUT OF SCOPE — do not build these:\n"
                     + "\n".join(f"- {f}" for f in out_of_scope))

    changelog = canvas.get("changelog") or []
    if changelog:
        parts.append("\nDecisions that were revised during the discussion:\n" + "\n".join(
            f"- {c.get('label') or c.get('field')}: now \"{c.get('to')}\""
            + (f" ({c.get('because')})" if c.get("because") else "")
            for c in changelog[-6:]))

    # Anything the discussion actually READ, so a claim in the brief can be traced back to
    # the page it came from rather than looking like the model's invention.
    refs, seen_ref = [], set()
    for m in (disc.get("messages") or []):
        for s in (m.get("sources") or []):
            u = s.get("url") or ""
            if s.get("ok") and u and u not in seen_ref:
                seen_ref.add(u)
                refs.append(f"- {u}" + (f" — {s.get('title')}" if s.get("title") else ""))
    if refs:
        parts.append("\nPages the owner pointed at during the discussion (context that "
                     "informed the decisions above):\n" + "\n".join(refs[:8]))

    transcript = []
    for m in (disc.get("messages") or []):
        role = "Owner" if m.get("role") == "user" else "Product lead"
        text = re.sub(r"\s+", " ", str(m.get("text") or "")).strip()
        if text:
            transcript.append(f"{role}: {text[:600]}")
    if transcript:
        body = "\n".join(transcript)
        parts.append("\nThe discussion this brief came from (context; the agreed points above "
                     "win if anything here contradicts them):\n" + body[:6000])

    brief = "\n".join(p for p in parts if p and p.strip()).strip()
    return brief or seed or "An app."


def render_vision(canvas: dict) -> str:
    """The canvas as markdown, for the inline "show me the vision" card and for a fallback
    reply when the model set `show` but wrote nothing."""
    lines = []
    v = field(canvas, "vision")
    name = field(canvas, "name")
    if name:
        lines.append(f"## {name}")
    if v:
        lines.append(v)
    for f in ("audience", "stack"):
        val = field(canvas, f)
        if val:
            lines.append(f"**{FIELD_LABELS[f]}:** {val}")
    for f in ("features", "out_of_scope"):
        val = field(canvas, f)
        if val:
            lines.append(f"**{FIELD_LABELS[f]}:**\n" + "\n".join(f"- {x}" for x in val))
    return "\n\n".join(lines)
