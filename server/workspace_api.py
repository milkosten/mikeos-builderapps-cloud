"""The WORKSPACE API (phase 32) — the shared work-tracker's HTTP surface.

**The product IS the API.** The Workspace tab in `/builder` is one client of it; the `ws`
tool in every assistant beat container is another; the build pipeline is a third (in-process,
through `workspace_store`). Anything else that can send an HTTP request and holds the
project's `workspace-api-key` is a fourth. Published in `/openapi.json` like every other
router here, so promoting this to a standalone `workspace-api.osmike.com` later is a routing
change, not a rewrite.

Paths are exactly the ones specced — `/api/projects/{pid}/items…` — precisely so that
promotion changes nothing for a client.

## Two credentials, one tenancy rule

* **The owner's session** — `Authorization: Bearer <account.osmike.com JWT>` (or the legacy
  `X-API-KEY`), ownership-checked against `projects.user_id`. This is the human in `/builder`.
* **`X-Workspace-Key: wsk_…`** — the per-project key the pipeline mints at create time and
  the scheduler injects into every assistant beat container as `WORKSPACE_API_KEY`. It is
  scoped to **exactly one project**. Used against any other project it does not get a 403; it
  gets **404**, because "forbidden" tells a caller the tenant exists and "not found" tells it
  nothing. Same contract as every other project route in this control plane.

## Attribution, and why the key alone is not enough

One key is shared by all of a project's assistants — that is the point (they are colleagues
sharing a tracker). But then the key cannot say *which* assistant is writing. So the `ws`
tool also sends the per-assistant `X-Assistant-Token` it already holds, and when that token
is valid **and belongs to the same project** the actor is resolved from it authoritatively:
`assistant:7 / Ada`. Without it the caller may still hint a name (`X-Actor-Name`), recorded
with `actor_kind=agent` — a hint, honestly labelled as one.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from server import assistants as A
from server import introspect, store
from server import workspace_store as W
from server.identity import authenticate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["workspace"])


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------
class CreateItemBody(BaseModel):
    """`kind` and `status` are FREE TEXT — deliberately no Enum and no validator.

    `feature | bug | task | testcase | doc | kb` and `open | in_progress | blocked | done |
    rejected` are the conventions the UI groups by, not a vocabulary this API polices. A team
    that wants a `risk` item or a `needs_review` status gets one with no deploy. Same rule as
    `assistants.role`.
    """
    kind: str = Field("task", max_length=40,
                      description="Free text. Conventionally feature|bug|task|testcase|doc|kb.")
    title: str = Field("", max_length=W.MAX_TITLE)
    body_md: str = Field("", max_length=W.MAX_BODY)
    status: str = Field("open", max_length=40,
                        description="Free text. Conventionally open|in_progress|blocked|done|rejected.")
    priority: str = Field("normal", max_length=20)
    assignee: str = Field("", max_length=120)


class PatchItemBody(BaseModel):
    """A partial update: every field is optional and `None` means 'leave it alone'."""
    kind: Optional[str] = Field(None, max_length=40)
    title: Optional[str] = Field(None, max_length=W.MAX_TITLE)
    body_md: Optional[str] = Field(None, max_length=W.MAX_BODY)
    status: Optional[str] = Field(None, max_length=40)
    priority: Optional[str] = Field(None, max_length=20)
    assignee: Optional[str] = Field(None, max_length=120)


class CommentBody(BaseModel):
    body_md: str = Field("", max_length=W.MAX_COMMENT)


class LinkBody(BaseModel):
    to_item: int = Field(..., description="The other item's id. Must be in the same project.")
    rel: str = Field("relates", max_length=40,
                     description="Free text. Conventionally covers|blocks|duplicates|relates.")


# ---------------------------------------------------------------------------
# auth: resolve {project, actor} from EITHER credential
# ---------------------------------------------------------------------------
async def _resolve(project_id: str, request: Request) -> W.Actor:
    """Authorize the caller for THIS project and return who they are.

    Order matters: the workspace key is checked first, because a beat container sends it and
    has no user session at all. A caller that sends neither credential gets 401; a caller
    that sends a good credential for a DIFFERENT project gets 404, never 403.
    """
    try:
        introspect.assert_shortid(project_id)
    except introspect.BadPath:
        raise HTTPException(status_code=404, detail="not found")

    key = (request.headers.get("x-workspace-key") or "").strip()
    if key:
        owner_pid = await W.project_for_key(key)
        if not owner_pid or owner_pid != project_id:
            # A key for another project must be indistinguishable from a bad key, and both
            # from a project that does not exist. This single line is the tenancy boundary.
            raise HTTPException(status_code=404, detail="not found")
        return await _actor_from_headers(request, project_id)

    user_id = await authenticate(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthorized")
    proj = await store.get_project(project_id, user_id)
    if not proj:
        raise HTTPException(status_code=404, detail="not found")
    return W.Actor.user(user_id)


async def _actor_from_headers(request: Request, project_id: str) -> W.Actor:
    """Name the machine caller behind a workspace key.

    The per-assistant token is authoritative when present (it is minted server-side, stored
    only as a hash, and carries its own project) — so an assistant cannot claim to be a
    different assistant. `X-Actor-Name` is a fallback hint for anything else holding the key,
    and is recorded as `agent`, never as `assistant`, so the trail never overstates what it
    knows.
    """
    tok = (request.headers.get("x-assistant-token") or "").strip()
    if tok:
        a = await A.get_by_token(tok)
        if a and str(a.get("project_id")) == project_id:
            return W.Actor.assistant(a["id"], a.get("name") or a.get("role") or "assistant")
    hint = (request.headers.get("x-actor-name") or "").strip()[:120]
    return W.Actor("agent", "agent", hint or "an agent")


async def _owner_only(project_id: str, request: Request) -> dict:
    """For the routes a shared key must NOT reach (revealing the key itself)."""
    user_id = await authenticate(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        introspect.assert_shortid(project_id)
    except introspect.BadPath:
        raise HTTPException(status_code=404, detail="not found")
    proj = await store.get_project(project_id, user_id)
    if not proj:
        raise HTTPException(status_code=404, detail="not found")
    return proj


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------
@router.get("/api/projects/{project_id}/items")
async def list_items(project_id: str, request: Request,
                     kind: str = Query("", max_length=40),
                     status: str = Query("", max_length=40),
                     assignee: str = Query("", max_length=120),
                     q: str = Query("", max_length=200),
                     limit: int = Query(200, ge=1, le=W.MAX_LIST),
                     offset: int = Query(0, ge=0)) -> dict:
    """This project's work items, filtered. Also returns the counts the UI's header shows and
    the CONVENTIONAL kind/status vocabularies — as hints for a client, not as a constraint:
    `kinds` is what exists plus the defaults, so a `risk` item invented by an assistant shows
    up in the UI's grouping without anybody deploying anything."""
    await _resolve(project_id, request)
    items = await W.list_items(project_id, kind=kind, status=status, assignee=assignee,
                               q=q, limit=limit, offset=offset)
    c = await W.counts(project_id)
    kinds = list(dict.fromkeys(W.DEFAULT_KINDS + sorted(c["by_kind"].keys())))
    statuses = list(dict.fromkeys(W.DEFAULT_STATUSES + sorted(c["by_status"].keys())))
    return {"items": items, "counts": c, "kinds": kinds, "statuses": statuses}


@router.post("/api/projects/{project_id}/items", status_code=201)
async def create_item(project_id: str, body: CreateItemBody, request: Request) -> dict:
    actor = await _resolve(project_id, request)
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="title is required")
    item = await W.create_item(project_id, actor, kind=body.kind, title=title,
                               body_md=body.body_md, status=body.status,
                               priority=body.priority, assignee=body.assignee)
    return {"ok": True, "item": item}


@router.get("/api/projects/{project_id}/items/{item_id}")
async def get_item(project_id: str, item_id: int, request: Request) -> dict:
    """The WHOLE item: fields + comments + event trail + links, in one call.

    Fat on purpose — see `workspace_store.full_item`. A phase-33 DM carries only an item id,
    and its recipient must be able to act on it without three more round trips."""
    await _resolve(project_id, request)
    item = await W.full_item(item_id, project_id)
    if not item:
        raise HTTPException(status_code=404, detail="not found")
    return {"item": item}


@router.patch("/api/projects/{project_id}/items/{item_id}")
async def patch_item(project_id: str, item_id: int, body: PatchItemBody,
                     request: Request) -> dict:
    """Update fields. Every genuine change writes an event naming the actor — that is how the
    user can see whether a human or a specific assistant moved something to `done`."""
    actor = await _resolve(project_id, request)
    item = await W.update_item(item_id, project_id, actor, **body.model_dump())
    if not item:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True, "item": item}


@router.post("/api/projects/{project_id}/items/{item_id}/comments", status_code=201)
async def comment_item(project_id: str, item_id: int, body: CommentBody,
                       request: Request) -> dict:
    actor = await _resolve(project_id, request)
    text = (body.body_md or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="body_md is required")
    c = await W.add_comment(item_id, project_id, actor, text)
    if not c:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True, "comment": c}


@router.get("/api/projects/{project_id}/items/{item_id}/events")
async def item_events(project_id: str, item_id: int, request: Request,
                      limit: int = Query(200, ge=1, le=500)) -> dict:
    """The audit trail: who did what, when, and from what to what."""
    await _resolve(project_id, request)
    if not await W.get_item(item_id, project_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"events": await W.events_for(item_id, project_id, limit)}


@router.post("/api/projects/{project_id}/items/{item_id}/links", status_code=201)
async def link_item(project_id: str, item_id: int, body: LinkBody, request: Request) -> dict:
    actor = await _resolve(project_id, request)
    link = await W.add_link(project_id, item_id, int(body.to_item), body.rel, actor)
    if not link:
        raise HTTPException(status_code=404,
                            detail="both items must exist in this project and differ")
    return {"ok": True, "link": link}


@router.get("/api/projects/{project_id}/search")
async def search(project_id: str, request: Request,
                 q: str = Query("", max_length=200),
                 kind: str = Query("", max_length=40),
                 limit: int = Query(50, ge=1, le=W.MAX_LIST)) -> dict:
    """Search across every kind at once — the knowledge-base lookup path.

    One table with a `kind` column is exactly why this is a single query: an assistant asking
    "what do we know about rate limiting?" gets the doc, the KB note, the bug and the test
    case together, which is what it actually wanted."""
    await _resolve(project_id, request)
    items = await W.list_items(project_id, q=q, kind=kind, limit=limit)
    return {"items": items, "q": q}


# ---------------------------------------------------------------------------
# the key itself — owner only
# ---------------------------------------------------------------------------
@router.get("/api/projects/{project_id}/workspace-key")
async def workspace_key(project_id: str, request: Request) -> dict:
    """The project's `workspace-api-key`, for the OWNER only (never reachable with the key
    itself — a shared credential must not be able to re-read itself out of the API).

    Minted on demand, so a project created before phase 32 gets one the first time anybody
    asks rather than being stranded without one.
    """
    await _owner_only(project_id, request)
    key = await W.ensure_key(project_id)
    return {"project_id": project_id, "workspace_api_key": key,
            "header": "X-Workspace-Key",
            "usage": f"curl -H 'X-Workspace-Key: {key[:8]}…' "
                     f"https://builderapps-api.osmike.com/api/projects/{project_id}/items"}
