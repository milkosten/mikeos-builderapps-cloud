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
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from server import discuss
from server.identity import authenticate

logger = logging.getLogger(__name__)

router = APIRouter()


class StartBody(BaseModel):
    seed: str = Field(..., min_length=1, max_length=4000)
    title: Optional[str] = None


class SayBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


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


async def _apply_turn(disc: dict, user_text: str = "") -> dict:
    """Run one model turn and persist it. The USER'S MESSAGE IS ALREADY IN `disc['messages']`
    when this is called, so a model failure loses the answer nowhere — it is written back with
    the apology attached rather than dropped on the floor."""
    result = await discuss.turn(disc, user_text)
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
    cursor is the "what would you like to build?" failure wearing a different hat."""
    uid = await _uid(request)
    disc = await discuss.create(uid, body.seed.strip(), (body.title or "").strip())
    try:
        disc = await _apply_turn(disc)
    except Exception as e:  # noqa: BLE001 — the room must exist even if the model was down
        logger.exception("opening turn failed for %s", disc["id"])
        messages = [{"role": "assistant", "ts": int(time.time() * 1000),
                     "text": "I couldn't reach the model to draft this just now — say "
                             "anything and I'll pick it up, or press Build it to go straight "
                             "to the pipeline.", "error": True}]
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
    answers. Chips post the option's text through this same path, so the thread records what
    was decided in the same words whether it was clicked or typed."""
    disc = await _owned(discussion_id, request)
    messages = list(disc.get("messages") or [])
    messages.append({"role": "user", "text": body.text.strip()[:4000],
                     "ts": int(time.time() * 1000)})
    disc = await discuss.save(disc["id"], messages=messages,
                              canvas=disc.get("canvas") or {},
                              title=disc.get("title") or "") or disc
    try:
        return await _apply_turn(disc, body.text.strip())
    except Exception as e:  # noqa: BLE001
        logger.exception("turn failed for %s", discussion_id)
        messages = list(disc.get("messages") or [])
        messages.append({"role": "assistant", "ts": int(time.time() * 1000), "error": True,
                         "text": "That didn't get through to the model — try again in a "
                                 f"moment. ({e})"[:400]})
        return await discuss.save(disc["id"], messages=messages,
                                  canvas=disc.get("canvas") or {},
                                  title=disc.get("title") or "") or disc


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
