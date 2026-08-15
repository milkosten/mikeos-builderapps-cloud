"""Phase 34 — the HTTP surface of the discussion room.

A router rather than more routes in `http_server`, for the same reason as assistants and the
workspace: the paths land in `/openapi.json` exactly as if they were declared on the app, and
the room's whole story stays in one file.

Every route is dual-authed through `authenticate` and OWNERSHIP-CHECKED — a discussion is a
private document (it contains someone's unbuilt product idea), so `get()` is always called
with the user id, never bare.

The one shape returned everywhere is the WHOLE discussion: {id, seed, title, messages, canvas,
status, project_id, cost_usd, turns}. The SPA restores a room from exactly the same payload it
gets after sending a message, so a hard reload and a live turn cannot disagree about the state
of the thread — the bug class that made the builder thread lose history three times.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from server import discuss
from server.identity import authenticate

logger = logging.getLogger(__name__)

router = APIRouter()


class StartBody(BaseModel):
    seed: str = Field(..., min_length=1, max_length=4000)
    title: Optional[str] = None


class AnswerBody(BaseModel):
    """One question and what came back for it. `skipped` is a FIRST-CLASS state, not merely
    the absence of text: the model is told "NOT ANSWERED" in those words, because the bug
    this replaced was precisely a missing answer being read as an answer."""
    q: str = Field("", max_length=300)
    answer: str = Field("", max_length=1200)
    skipped: bool = False


class SayBody(BaseModel):
    """Either free text (the composer) or a whole ANSWER SET (the questionnaire) — never
    half a set. The stepper holds every answer client-side until Submit, so one submit is
    one user turn and the model always sees the complete picture of what it asked."""
    text: str = Field("", max_length=4000)
    answers: Optional[List[AnswerBody]] = None


class DraftBody(BaseModel):
    """The half-filled questionnaire. Free-form on purpose — it is the client's own scratch
    state (which step, which chips, what has been typed) and the server's only job is to hand
    it back unchanged after a reload. It NEVER reaches the model and never becomes a turn."""
    draft: dict = Field(default_factory=dict)


async def _uid(request: Request) -> str:
    uid = await authenticate(request)
    if not uid:
        raise HTTPException(status_code=401, detail="unauthorized")
    return uid


async def _owned(discussion_id: str, request: Request) -> dict:
    uid = await _uid(request)
    disc = await discuss.get(discussion_id, uid)
    if not disc:
        raise HTTPException(status_code=404, detail="not found")
    return disc


def _title_for(disc: dict) -> str:
    """A room needs a name in the Apps list from its first turn. The canvas name is the real
    one; until the model has proposed one, the seed truncated is honest and stable."""
    name = discuss.field(disc.get("canvas") or {}, "name")
    if name:
        return str(name)[:120]
    seed = (disc.get("seed") or "").strip()
    return (seed[:60] + ("…" if len(seed) > 60 else "")) or "Untitled discussion"


async def _apply_turn(disc: dict, user_text: str = "",
                      answers: Optional[list] = None) -> dict:
    """Run one model turn and persist it. The USER'S MESSAGE IS ALREADY IN `disc['messages']`
    when this is called, so a model failure loses the answer nowhere — it is written back with
    the apology attached rather than dropped on the floor."""
    result = await discuss.turn(disc, user_text, answers)
    messages = list(disc.get("messages") or [])
    entry = {
        "role": "assistant",
        "text": result["reply"],
        "ts": int(time.time() * 1000),
    }
    if result.get("questions"):
        entry["questions"] = result["questions"]
    if result.get("sources"):
        # PROVENANCE, rendered in the thread as "read example.com/article (1,240 words)". A
        # model that silently absorbs a page and then asserts facts is indistinguishable from
        # one that invented them — so what it read (and what it was REFUSED) is part of the
        # record, not a log line nobody will look at.
        entry["sources"] = result["sources"]
    if result.get("show"):
        # A SNAPSHOT, not a flag. Rendering "the vision" on reload from the CURRENT canvas
        # would rewrite history every time a later turn changed it — the thread must keep
        # saying what it actually said at the time.
        entry["show"] = result["show"]
        entry["shown"] = discuss.render_vision(result["canvas"])
    messages.append(entry)
    saved = await discuss.save(
        disc["id"], messages=messages, canvas=result["canvas"],
        title=_title_for({**disc, "canvas": result["canvas"]}),
        cost_delta=float(result.get("cost_usd") or 0), turn_delta=1)
    return saved or disc


@router.post("/api/discussions")
async def start_discussion(body: StartBody, request: Request):
    """Open a room from the sentence typed on the start page, and take the FIRST TURN before
    replying — the user pressed Discuss to see a proposal, and an empty room with a blinking
    cursor is the "what would you like to build?" failure wearing a different hat.

    THE USER'S SENTENCE IS THE FIRST MESSAGE, persisted before the model is called. It used
    to live only in the `seed` column, so the thread began with the assistant answering a
    question that was nowhere on the screen — and a reload, or a shared /discuss/<id> link,
    showed a proposal with no visible ask behind it. It is a message; it belongs in the
    messages."""
    uid = await _uid(request)
    seed = body.seed.strip()
    disc = await discuss.create(uid, seed, (body.title or "").strip())
    opening = [{"role": "user", "text": seed[:4000], "ts": int(time.time() * 1000)}]
    disc = await discuss.save(disc["id"], messages=opening, canvas={},
                              title=_title_for(disc)) or disc
    try:
        disc = await _apply_turn(disc)
    except Exception as e:  # noqa: BLE001 — the room must exist even if the model was down
        logger.exception("opening turn failed for %s", disc["id"])
        messages = opening + [{"role": "assistant", "ts": int(time.time() * 1000),
                               "text": "I couldn't reach the model to draft this just now — "
                                       "say anything and I'll pick it up, or press Build it "
                                       "to go straight to the pipeline.", "error": True}]
        disc = await discuss.save(disc["id"], messages=messages, canvas={},
                                  title=_title_for(disc)) or disc
        logger.warning("discussion %s opened without a draft: %s", disc["id"], e)
    return disc


@router.get("/api/discussions")
async def list_discussions(request: Request):
    """The user's drafts, for the Apps list. Deliberately a SEPARATE list from /api/projects:
    a draft has no URL, no status pill and nothing to open in the builder, and merging the two
    is how a conversation ends up rendered as a broken app."""
    uid = await _uid(request)
    return {"discussions": await discuss.list_for(uid)}


@router.get("/api/discussions/{discussion_id}")
async def get_discussion(discussion_id: str, request: Request):
    return await _owned(discussion_id, request)


@router.post("/api/discussions/{discussion_id}/messages")
async def say(discussion_id: str, body: SayBody, request: Request):
    """One exchange: the user's message is appended and persisted FIRST, then the model
    answers.

    TWO SHAPES, ONE TURN. Free text is the composer. `answers` is the whole questionnaire,
    submitted once — it is stored BOTH as a readable user message (so the transcript honestly
    shows every question and what was chosen, skips included) AND as the structured set, so
    the model can be told which questions have no answer instead of guessing.

    What is NOT here any more is the old chip path, where clicking one option posted it as a
    complete user turn: the model then saw four questions asked and one answer back, invented
    the other three and marked them decided — and an agreed canvas cell can only be undone by
    an explicit revision, so the guesses stuck."""
    disc = await _owned(discussion_id, request)
    answers = discuss.clean_answers([a.model_dump() for a in body.answers]) \
        if body.answers else []
    text = discuss.answers_text(answers) if answers else body.text.strip()[:4000]
    if not text:
        raise HTTPException(status_code=422, detail="a message or an answer set is required")

    entry = {"role": "user", "text": text, "ts": int(time.time() * 1000)}
    if answers:
        entry["answers"] = answers
    messages = list(disc.get("messages") or [])
    messages.append(entry)
    disc = await discuss.save(disc["id"], messages=messages,
                              canvas=disc.get("canvas") or {},
                              title=disc.get("title") or "") or disc
    if answers:
        # The questionnaire has been submitted; its scratchpad is spent. Clearing it here
        # rather than from the browser means a submit that raced a reload cannot resurrect a
        # half-filled form over the answers that were actually sent.
        try:
            await discuss.save_draft_answers(disc["id"], {})
        except Exception:  # noqa: BLE001 — a stale scratchpad must never fail a real turn
            logger.warning("could not clear the answer draft for %s", discussion_id)
    try:
        return await _apply_turn(disc, "" if answers else text, answers or None)
    except Exception as e:  # noqa: BLE001
        logger.exception("turn failed for %s", discussion_id)
        messages = list(disc.get("messages") or [])
        messages.append({"role": "assistant", "ts": int(time.time() * 1000), "error": True,
                         "text": "That didn't get through to the model — try again in a "
                                 f"moment. ({e})"[:400]})
        return await discuss.save(disc["id"], messages=messages,
                                  canvas=disc.get("canvas") or {},
                                  title=disc.get("title") or "") or disc


@router.put("/api/discussions/{discussion_id}/answer_draft")
async def save_answer_draft(discussion_id: str, body: DraftBody, request: Request):
    """Persist the questionnaire IN PROGRESS — which step, which chips, what has been typed.

    Deliberately NOT a turn. It writes one scratch column and nothing else: no message, no
    canvas, no cost, no `updated_at`, and the model is never invoked. It exists so that a
    reload three answers deep restores like everything else in this room does, without
    weakening the promise that nothing reaches the model until Submit."""
    disc = await _owned(discussion_id, request)
    try:
        await discuss.save_draft_answers(disc["id"], body.draft or {})
    except ValueError:
        raise HTTPException(status_code=413, detail="answer draft too large")
    return {"ok": True}


@router.get("/api/discussions/{discussion_id}/brief")
async def brief(discussion_id: str, request: Request):
    """The exact text that will be sent to the create-pipeline. Its own endpoint because
    "what actually reached the pipeline" must be inspectable BEFORE the build, not inferred
    afterwards from the docs it produced."""
    disc = await _owned(discussion_id, request)
    return {"id": disc["id"], "brief": discuss.compose_brief(disc),
            "title": discuss.field(disc.get("canvas") or {}, "name") or disc.get("title") or ""}


@router.delete("/api/discussions/{discussion_id}")
async def delete_discussion(discussion_id: str, request: Request):
    uid = await _uid(request)
    if not await discuss.delete(discussion_id, uid):
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True, "deleted": discussion_id}
