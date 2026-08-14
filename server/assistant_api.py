"""HTTP surface for per-project AI assistants (phase 29).

Kept as its OWN `APIRouter` rather than more routes in `http_server.py`: assistants are a
self-contained subsystem, and FastAPI publishes a router's routes in `/openapi.json` exactly
as if they were declared on the app — so the OpenAPI contract is complete either way.

Two audiences, two auth models, deliberately separated:

* **`/api/projects/{id}/assistants…` — the OWNER.** Dual-auth (account.osmike.com Bearer or
  legacy X-API-KEY) and ownership-checked through `_owned()`, exactly like every other
  project route. Another user's assistant is indistinguishable from a missing one: 404.

* **`/api/assistant/…` — the BEAT CONTAINER.** Authenticated by `X-Assistant-Token`, a
  credential minted per assistant and scoped to exactly {project, assistant}. It carries no
  user session and can reach nothing except its own assistant's project. This is the surface
  where `assistants.require()` decides what the beat may actually do — the container's own
  capability list is a fail-fast convenience, this is the enforcement.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from server import assistants as A
from server import assistant_runtime as R
from server import browser_proxy, introspect, llm_proxy, store
from server.identity import authenticate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["assistants"])


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------
class CreateAssistantBody(BaseModel):
    """`role` is FREE TEXT. `template` only pre-fills the fields you omit — it never limits
    what an assistant can be."""
    role: Optional[str] = Field(None, max_length=120,
                                description="Free text. Any role, not an enum.")
    name: Optional[str] = Field(None, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    soul_md: Optional[str] = Field(None, max_length=A.MAX_SOUL_CHARS)
    capabilities: Optional[list[str]] = None
    interval_minutes: Optional[int] = Field(None, ge=1, le=60 * 24 * 7)
    template: Optional[str] = Field(None, max_length=64,
                                    description="Starter template key to pre-fill from.")
    start: bool = Field(False, description="Start beating immediately.")


class PatchAssistantBody(BaseModel):
    role: Optional[str] = Field(None, max_length=120)
    name: Optional[str] = Field(None, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    soul_md: Optional[str] = Field(None, max_length=A.MAX_SOUL_CHARS)
    capabilities: Optional[list[str]] = None
    interval_minutes: Optional[int] = Field(None, ge=1, le=60 * 24 * 7)


class ReasonBody(BaseModel):
    workspace: str = Field("", max_length=40000)
    docs: str = Field("", max_length=60000,
                      description="The project's own strategy docs, loaded deterministically "
                                  "by the beat program. They are the grounding the SOUL "
                                  "sits on top of.")


class ActBody(BaseModel):
    action: dict = Field(default_factory=dict)


class BeatNowBody(BaseModel):
    """`@Developer add a search box` — a human addressing one assistant from the composer.

    `task` is optional: an empty body is the plain "beat now" button, which lets the
    assistant decide for itself what is worth doing."""
    task: Optional[str] = Field(None, max_length=A.MAX_ASK_CHARS)


class ActivityBody(BaseModel):
    """What the assistant is doing, streamed out of the beat container as it happens.

    Every line corresponds to something that ACTUALLY occurred — a tool the coding agent
    invoked, a file it wrote, a commit, a health gate verdict. Nothing here is a plausible-
    looking progress message invented to fill the gap; a feed that narrates work it did not
    observe is worse than a quiet one.
    """
    lines: list[dict] = Field(default_factory=list, max_length=200)


class BeatBody(BaseModel):
    status: str = Field("done", max_length=16)
    thought: str = Field("", max_length=8000)
    actions: list[dict] = Field(default_factory=list)
    log: str = Field("", max_length=20000)
    tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# owner-facing helpers
# ---------------------------------------------------------------------------
async def _owned(project_id: str, request: Request) -> dict:
    """Authenticate, validate the shortid shape, confirm ownership. Same contract as the
    introspection routes — the id is validated BEFORE it can reach a query or an argv."""
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


async def _owned_assistant(project_id: str, assistant_id: int, request: Request) -> dict:
    await _owned(project_id, request)
    a = await A.get(assistant_id, project_id)
    if not a:
        raise HTTPException(status_code=404, detail="assistant not found")
    return a


def _public(a: dict, *, last: Optional[dict] = None) -> dict:
    """The client view. `token_enc`/`token_sha` are never selected, let alone returned."""
    out = {
        "id": int(a["id"]), "project_id": a["project_id"], "role": a.get("role") or "",
        "name": a.get("name") or "", "description": a.get("description") or "",
        "capabilities": a.get("capabilities") or [],
        "interval_minutes": int(a.get("interval_minutes") or 60),
        "status": a.get("status") or "paused",
        "soul_path": f"docs/assistants/{A.slug(a.get('role') or 'assistant')}.SOUL.md",
        "last_beat_at": a.get("last_beat_at"), "next_beat_at": a.get("next_beat_at"),
        "beating": bool((a.get("beat_owner") or "").strip()),
        "created_at": a.get("created_at"),
    }
    if last is not None:
        out["last_beat"] = last
    return out


# ===========================================================================
# catalog — templates + capabilities. Separate path (not /assistants/templates)
# because {assistant_id} is an int and a word there would 422.
# ===========================================================================
@router.get("/api/assistants/catalog", summary="Starter templates + the capability vocabulary")
async def assistants_catalog() -> dict:
    """The six starter templates and the enforced capability set.

    The templates are PRE-FILLS the user edits — the API accepts any `role` string, and this
    endpoint exists so the UI can offer a helpful starting point, not a menu of allowed roles.
    """
    return {
        "templates": A.template_list(),
        "capabilities": [
            {"id": cid, **{k: v for k, v in meta.items()}} for cid, meta in A.CAPABILITIES.items()
        ],
        "limits": {"min_interval_minutes": A.MIN_INTERVAL_MIN,
                   "max_interval_minutes": A.MAX_INTERVAL_MIN,
                   "max_per_project": A.MAX_ASSISTANTS_PER_PROJECT},
        "roles_are_open_ended": True,
    }


# ===========================================================================
# owner-facing CRUD
# ===========================================================================
@router.get("/api/projects/{project_id}/assistants", summary="List a project's assistants")
async def list_assistants(project_id: str, request: Request) -> dict:
    await _owned(project_id, request)
    rows = await A.list_for_project(project_id)
    out = []
    for a in rows:
        out.append(_public(a, last=await A.last_beat(int(a["id"]))))
    return {"assistants": out}


@router.post("/api/projects/{project_id}/assistants", summary="Start a new assistant")
async def create_assistant(project_id: str, body: CreateAssistantBody,
                           request: Request) -> dict:
    await _owned(project_id, request)
    if await A.count_for_project(project_id) >= A.MAX_ASSISTANTS_PER_PROJECT:
        raise HTTPException(status_code=400,
                            detail=f"a project may have at most "
                                   f"{A.MAX_ASSISTANTS_PER_PROJECT} assistants")

    tpl = A.TEMPLATES_BY_KEY.get((body.template or "").strip()) or {}
    role = (body.role or tpl.get("role") or "").strip()
    if not role:
        raise HTTPException(status_code=400, detail="role is required (any text you like)")
    name = (body.name or tpl.get("name") or role).strip()
    description = (body.description if body.description is not None
                   else tpl.get("description", "")) or ""
    soul = (body.soul_md if body.soul_md is not None else tpl.get("soul_md")) or \
        A.default_soul(role, name, description)
    caps = A.sanitize_capabilities(
        body.capabilities if body.capabilities is not None else tpl.get("capabilities", []))
    interval = A.clamp_interval(
        body.interval_minutes if body.interval_minutes is not None
        else tpl.get("interval_minutes"), 60)

    a = await A.create(project_id=project_id, role=role, name=name, description=description,
                       soul_md=soul, capabilities=caps, interval_minutes=interval,
                       status="active" if body.start else "paused")
    logger.info("assistant %s (%s) created for %s with caps %s",
                a["id"], role, project_id, caps)
    return _public(a)


@router.get("/api/projects/{project_id}/assistants/{assistant_id}",
            summary="One assistant + its recent beats")
async def get_assistant(project_id: str, assistant_id: int, request: Request) -> dict:
    a = await _owned_assistant(project_id, assistant_id, request)
    out = _public(a, last=await A.last_beat(assistant_id))
    out["soul_md"] = a.get("soul_md") or ""
    out["beats"] = await A.list_beats(assistant_id, 20)
    return out


@router.patch("/api/projects/{project_id}/assistants/{assistant_id}",
              summary="Edit an assistant's SOUL, role, capabilities or interval")
async def patch_assistant(project_id: str, assistant_id: int, body: PatchAssistantBody,
                          request: Request) -> dict:
    await _owned_assistant(project_id, assistant_id, request)
    a = await A.update(assistant_id, project_id, **body.model_dump(exclude_unset=True))
    if not a:
        raise HTTPException(status_code=404, detail="assistant not found")
    return _public(a)


@router.delete("/api/projects/{project_id}/assistants/{assistant_id}",
               summary="Delete an assistant and its beat history")
async def delete_assistant(project_id: str, assistant_id: int, request: Request) -> dict:
    await _owned_assistant(project_id, assistant_id, request)
    ok = await A.delete(assistant_id, project_id)
    if ok:
        R.cleanup_workspace(assistant_id)
    return {"ok": ok}


@router.post("/api/projects/{project_id}/assistants/{assistant_id}/start",
             summary="Resume the heartbeat")
async def start_assistant(project_id: str, assistant_id: int, request: Request) -> dict:
    await _owned_assistant(project_id, assistant_id, request)
    a = await A.set_status(assistant_id, project_id, "active")
    if not a:
        raise HTTPException(status_code=404, detail="assistant not found")
    return _public(a)


@router.post("/api/projects/{project_id}/assistants/{assistant_id}/pause",
             summary="Pause the heartbeat")
async def pause_assistant(project_id: str, assistant_id: int, request: Request) -> dict:
    await _owned_assistant(project_id, assistant_id, request)
    a = await A.set_status(assistant_id, project_id, "paused")
    if not a:
        raise HTTPException(status_code=404, detail="assistant not found")
    return _public(a)


@router.post("/api/projects/{project_id}/assistants/{assistant_id}/beat",
             summary="Run one beat right now")
async def beat_assistant(project_id: str, assistant_id: int, request: Request,
                         body: Optional[BeatNowBody] = None) -> dict:
    """Kick one beat immediately.

    Returns as soon as the beat row exists — a beat takes tens of seconds (a container start
    plus an LLM round), and blocking an HTTP request on that would just time out behind
    Caddy. The client polls `/beats`; the row is already there, marked `running`.
    """
    a = await _owned_assistant(project_id, assistant_id, request)
    kicked = await R.kick(a, (body.task if body and body.task else "").strip())
    if not kicked:
        raise HTTPException(status_code=409,
                            detail="a beat is already running for this assistant")
    claimed, beat_id = kicked
    # Background, with a strong ref held by the runner's task set (a bare create_task can be
    # garbage-collected mid-flight — this codebase has been bitten by that).
    from server.http_server import _spawn
    _spawn(R.execute_beat(claimed, beat_id, "manual"))
    return {"ok": True, "beat_id": beat_id, "status": "running"}


@router.get("/api/projects/{project_id}/assistants/{assistant_id}/beats",
            summary="Beat history (thought, actions, tokens, cost)")
async def assistant_beats(project_id: str, assistant_id: int, request: Request,
                          limit: int = 40) -> dict:
    await _owned_assistant(project_id, assistant_id, request)
    return {"beats": await A.list_beats(assistant_id, limit)}


@router.get("/api/projects/{project_id}/assistant-activity",
            summary="Live + restorable feed of what this project's assistants are doing")
async def project_assistant_activity(project_id: str, request: Request,
                                     limit: int = 6) -> dict:
    """The /builder left pane's assistant feed — BOTH the live view and the reload restore.

    One mechanism, deliberately: the pane polls this while a beat is running and reads it
    once on load, so what you see mid-beat and what you see after a hard refresh are the
    same rows from the same table. A separate live channel would be a second source of
    truth, and the one that drifts is always the one nobody reloads to check.
    """
    await _owned(project_id, request)
    feed = await A.recent_activity(project_id, limit)
    return {"beats": feed, "beating": any(b.get("status") == "running" for b in feed)}


@router.get("/api/projects/{project_id}/assistants/{assistant_id}/soul",
            summary="The raw SOUL.md")
async def assistant_soul(project_id: str, assistant_id: int, request: Request) -> dict:
    a = await _owned_assistant(project_id, assistant_id, request)
    return {"path": f"docs/assistants/{A.slug(a.get('role') or 'assistant')}.SOUL.md",
            "markdown": a.get("soul_md") or ""}


# ===========================================================================
# beat-container-facing. X-Assistant-Token only; no user session, no cookies.
# ===========================================================================
async def _from_token(token: Optional[str]) -> dict:
    a = await A.get_by_token((token or "").strip())
    if not a:
        raise HTTPException(status_code=401, detail="invalid assistant token")
    return a


@router.get("/api/assistant/context", summary="[beat container] perceive the project")
async def assistant_context(x_assistant_token: str = Header("")) -> dict:
    a = await _from_token(x_assistant_token)
    # include_soul: the container mirrors it into the repo. `reason` deliberately does NOT
    # include it — there it is already the system prompt.
    return await R.perceive(a, include_soul=True)


@router.post("/api/assistant/reason", summary="[beat container] one LLM round")
async def assistant_reason(body: ReasonBody, request: Request,
                           x_assistant_token: str = Header("")) -> dict:
    """The model call happens HERE, not in the container — so no model credential ever
    lives inside an LLM-driven container, and the tokens land in the project's own
    accounting."""
    a = await _from_token(x_assistant_token)
    ctx = await R.perceive(a)
    # The human's instruction is read from the BEAT ROW, not from the container's request
    # body: it decides what this beat spends the project's money on, so it comes from the
    # row the owner's authenticated call created.
    raw = (request.headers.get("x-beat-id") or "").strip()
    ask = ""
    if raw.isdigit() and await A.beat_belongs_to(int(raw), int(a["id"])):
        ask = await A.beat_ask(int(raw))
    return await R.reason(a, ctx, body.workspace or "", body.docs or "", ask)


@router.post("/api/assistant/act", summary="[beat container] perform one capability-gated act")
async def assistant_act(body: ActBody, request: Request,
                        x_assistant_token: str = Header("")) -> dict:
    """THE enforcement point. `apply_action` calls `assistants.require()` for every act, so a
    capability the assistant was not granted is refused here regardless of what the container
    (or its SOUL) believes."""
    a = await _from_token(x_assistant_token)
    raw = (request.headers.get("x-beat-id") or "").strip()
    beat_id = int(raw) if raw.isdigit() and await A.beat_belongs_to(int(raw), int(a["id"])) \
        else None
    return await R.apply_action(a, body.action or {}, beat_id=beat_id)


@router.post("/api/assistant/activity",
             summary="[beat container] stream what the assistant is doing, live")
async def assistant_activity(body: ActivityBody, request: Request,
                             x_assistant_token: str = Header("")) -> dict:
    """Append activity lines to the beat that is running right now.

    Called every couple of seconds by the beat container while its coding agent works, so
    the /builder left pane fills in as the work happens instead of staying blank for two
    minutes and then dumping a result. A beat that is already closed is not written to — a
    late batch is dropped, not resurrected.
    """
    a = await _from_token(x_assistant_token)
    raw = (request.headers.get("x-beat-id") or "").strip()
    if not raw.isdigit() or not await A.beat_belongs_to(int(raw), int(a["id"])):
        raise HTTPException(status_code=403, detail="that beat is not yours")
    try:
        total = await A.append_activity(int(raw), body.lines or [])
    except RuntimeError:
        raise HTTPException(status_code=404, detail="no such beat")
    return {"ok": True, "lines": total}


@router.get("/api/assistant/deploy/{run_id}",
            summary="[beat container] how did the ship-HEAD run it started end?")
async def assistant_deploy_status(run_id: int, x_assistant_token: str = Header("")) -> dict:
    """The beat polls this so its record carries the REAL outcome of the deploy it caused.

    Without it a beat would report "done" the instant it asked for a deploy, and a health
    gate that went red ten minutes later would be invisible in the timeline — the "never
    trust a 200" mistake, wearing a different hat.
    """
    a = await _from_token(x_assistant_token)
    return await R.deploy_status(a, run_id)


@router.post("/api/assistant/beat", summary="[beat container] record the finished beat")
async def assistant_record_beat(body: BeatBody, request: Request,
                                x_assistant_token: str = Header("")) -> dict:
    a = await _from_token(x_assistant_token)
    raw = (request.headers.get("x-beat-id") or "").strip()
    beat_id: Optional[int] = None
    if raw.isdigit():
        # A beat container may only close ITS OWN beat — a token is scoped to one assistant,
        # and a beat id is not a capability.
        if not await A.beat_belongs_to(int(raw), int(a["id"])):
            raise HTTPException(status_code=403, detail="that beat is not yours")
        beat_id = int(raw)
    if beat_id is None:
        beats = await A.list_beats(int(a["id"]), 1)
        running = [b for b in beats if b.get("status") == "running"]
        if not running:
            raise HTTPException(status_code=409, detail="no running beat to record")
        beat_id = int(running[0]["id"])
    status = body.status if body.status in ("done", "skipped", "failed") else "done"
    # The container can only report what IT knows it spent. Everything Pi spent went through
    # the control plane's LLM proxy, which is the only place that saw the real numbers — add
    # it here so the beat's cost is the beat's whole cost, not just the part the container
    # happened to be told about.
    pi_cost = llm_proxy.forget_beat(beat_id)
    # THE BACKSTOP FOR BROWSER SESSIONS. The beat closes its own, but a container that is
    # OOM-killed or times out mid-navigation never gets to; chrome-pool is a shared fleet with
    # a fixed number of Chromes, so a leak here is everyone else's outage. Closing at the one
    # point every beat passes through means a leak needs BOTH the container and this to fail.
    try:
        await browser_proxy.close_beat_sessions(beat_id)
    except Exception:  # noqa: BLE001 — never lose a beat record over a browser cleanup
        logger.debug("browser session cleanup failed for beat %s", beat_id, exc_info=True)
    await A.finish_beat(beat_id, status=status, thought=body.thought,
                        actions=body.actions, log=body.log, tokens=body.tokens,
                        cost_usd=float(body.cost_usd or 0.0) + pi_cost,
                        duration_ms=body.duration_ms)
    return {"ok": True, "beat_id": beat_id, "coding_agent_cost_usd": pi_cost}
