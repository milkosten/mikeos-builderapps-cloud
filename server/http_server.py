"""mikeos-builderapps-cloud — the control plane behind builderapps-api.osmike.com.

Prompt -> a REAL deployed full-stack app at https://<shortid>.builderapps.osmike.com/.
FastAPI + asyncpg (its own Postgres), OAuth dual-auth against account.osmike.com, talks to
Gitea + the Docker socket. This MVP proves the spine: create-pipeline stands up a live,
routed, healthy skeleton per project.

SSE contract (streaming endpoints): media_type=text/event-stream, each event a line
`data: {json}\\n\\n` with a `type` in {run_start,step_start,step_done,progress,repo,deploy,
done,error}.
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from server import db, gitea, store
from server.harness import pipeline
from server.identity import authenticate, current_user

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("builderapps")

PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "https://builderapps.osmike.com")
SITES_BASE = os.environ.get("SITES_BASE", "builderapps.osmike.com")

# Hold strong refs to background tasks so they are never GC'd mid-flight (designer bug).
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    logger.info("builderapps-cloud up; public=%s sites=%s", PUBLIC_BASE, SITES_BASE)
    yield
    await db.close_pool()


app = FastAPI(title="mikeos-builderapps-cloud", lifespan=lifespan)

_CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS", "https://builderapps.osmike.com").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware, allow_origins=_CORS_ORIGINS, allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


# ---- background gitea provisioning (phase 10) -----------------------------
async def _ensure_gitea_bg(user_id: str, email: Optional[str]) -> None:
    """Fire-and-forget, idempotent + self-healing. Never surfaces errors to the UI."""
    try:
        existing = await store.get_gitea_account(user_id)
        if existing and await gitea._get_user(existing["gitea_username"]):
            return
        await gitea.ensure_user(user_id, email)
        logger.info("background gitea account ensured for %s", user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("background gitea provisioning failed for %s: %s", user_id, e)


async def _auth_and_provision(request: Request) -> str:
    """Authenticate, and on the first authed call kick off gitea provisioning in the bg."""
    uid = await authenticate(request)
    if not uid:
        raise HTTPException(status_code=401, detail="unauthorized")
    email = None
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        try:
            import jwt as _jwt
            claims = _jwt.decode(auth[7:].strip(), options={"verify_signature": False})
            email = claims.get("email")
        except Exception:
            pass
    _spawn(_ensure_gitea_bg(uid, email))
    request.state.user_email = email
    return uid


# ---- request bodies -------------------------------------------------------
class CreateBody(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)
    title: Optional[str] = None


class UpdateBody(BaseModel):
    request: str = Field(..., min_length=1, max_length=8000)


class MessagesBody(BaseModel):
    """The durable chat thread. Sanitized server-side (store.sanitize_messages) — the client
    may send anything; only {role,text} survives."""
    messages: list[dict] = Field(default_factory=list)


# ---- SSE plumbing ---------------------------------------------------------
def _sse(item: dict) -> str:
    return f"data: {json.dumps(item)}\n\n"


# ---- health ---------------------------------------------------------------
@app.get("/api/health")
async def health():
    database = "unknown"
    try:
        await db.pool().fetchval("SELECT 1")
        database = "ok"
    except Exception as e:  # noqa: BLE001
        database = f"error: {e}"
    return {"status": "ok", "database": database}


# ---- internal: Caddy on-demand TLS allow-gate (harmless if unused) --------
@app.get("/internal/caddy/tls-allow")
async def tls_allow(domain: str = ""):
    """Return 200 only for a known live/creating project subdomain (guards on-demand TLS).
    The current wildcard cert path doesn't use this, but it's here for the on-demand mode."""
    host = domain.strip().lower()
    suffix = f".{SITES_BASE}"
    if not host.endswith(suffix):
        raise HTTPException(status_code=404, detail="not a builderapps host")
    shortid = host[: -len(suffix)]
    proj = await store.get_project(shortid)
    if not proj:
        raise HTTPException(status_code=404, detail="unknown project")
    return {"ok": True}


# ---- create project -------------------------------------------------------
@app.post("/api/projects")
async def create_project(body: CreateBody, request: Request):
    """Start the create-pipeline. Returns the project id immediately and streams the run's
    steps as SSE. The project + repo binding are created up-front; the pipeline runs inline
    on this request so the client sees live progress."""
    user_id = await _auth_and_provision(request)
    email = getattr(request.state, "user_email", None)

    shortid = await store.alloc_shortid()
    gitea_user = gitea.gitea_username_for(user_id)
    repo_name = f"app-{shortid}"
    title = (body.title or body.prompt[:60]).strip()

    await store.create_project(
        id=shortid, user_id=user_id, gitea_owner=gitea_user, gitea_repo=repo_name,
        subdomain=f"{shortid}.{SITES_BASE}", title=title, prompt=body.prompt,
        status="creating", pipeline="create",
    )
    run_id = await store.create_run(shortid, "create", body.prompt, total_steps=7)

    async def event_gen():
        q: asyncio.Queue = asyncio.Queue()

        # initial event so the client immediately learns the id + url
        await q.put({"type": "created", "id": shortid,
                     "url": f"https://{shortid}.{SITES_BASE}/", "run_id": run_id})

        async def body_task():
            try:
                await pipeline.run_create(shortid, run_id, user_id, email, q.put_nowait)
            except Exception as e:  # noqa: BLE001
                logger.exception("create pipeline failed for %s", shortid)
                q.put_nowait({"type": "error", "message": str(e)})
            finally:
                q.put_nowait(None)

        task = _spawn(body_task())
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield _sse(item)
        finally:
            await task

    return StreamingResponse(
        event_gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"})


# ---- update project (phase 18) --------------------------------------------
@app.post("/api/projects/{project_id}/update")
async def update_project(project_id: str, body: UpdateBody, request: Request):
    """Apply a natural-language change to an existing app (ownership-checked). Streams the
    update run as SSE, same vocabulary as create."""
    user_id = await _auth_and_provision(request)
    email = getattr(request.state, "user_email", None)

    proj = await store.get_project(project_id, user_id)   # ownership-checked
    if not proj:
        raise HTTPException(status_code=404, detail="not found")

    await store.set_project_status(project_id, "building")
    run_id = await store.create_run(project_id, "update", body.request, total_steps=5)

    async def event_gen():
        q: asyncio.Queue = asyncio.Queue()
        await q.put({"type": "created", "id": project_id,
                     "url": f"https://{project_id}.{SITES_BASE}/", "run_id": run_id})

        async def body_task():
            try:
                await pipeline.run_update(project_id, run_id, user_id, email,
                                          body.request, q.put_nowait)
            except Exception as e:  # noqa: BLE001
                logger.exception("update pipeline failed for %s", project_id)
                q.put_nowait({"type": "error", "message": str(e)})
            finally:
                q.put_nowait(None)

        task = _spawn(body_task())
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield _sse(item)
        finally:
            await task

    return StreamingResponse(
        event_gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"})


# ---- list / get -----------------------------------------------------------
@app.get("/api/projects")
async def list_projects(request: Request):
    user_id = await _auth_and_provision(request)
    return {"projects": await store.list_projects(user_id)}


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str, request: Request):
    """The full restore payload for /builder/<id>: the project, its latest run WITH the
    ordered `steps` already executed, and the durable chat `messages`. A page reload rebuilds
    history from here (Postgres), never from browser storage."""
    user_id = await _auth_and_provision(request)
    proj = await store.get_project(project_id, user_id)
    if not proj:
        raise HTTPException(status_code=404, detail="not found")
    run = await store.latest_run_for(project_id)   # includes steps ordered by idx
    proj["latest_run"] = run
    proj["messages"] = await store.get_messages(project_id)
    proj["url"] = f"https://{project_id}.{SITES_BASE}/"
    return proj


@app.get("/api/projects/{project_id}/steps")
async def get_project_steps(project_id: str, request: Request):
    """Step history of the project's latest run, for independent polling/refresh."""
    user_id = await _auth_and_provision(request)
    if not await store.get_project(project_id, user_id):
        raise HTTPException(status_code=404, detail="not found")
    return await store.steps_for_latest_run(project_id)


@app.get("/api/projects/{project_id}/messages")
async def get_project_messages(project_id: str, request: Request):
    user_id = await _auth_and_provision(request)
    if not await store.get_project(project_id, user_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"messages": await store.get_messages(project_id)}


@app.put("/api/projects/{project_id}/messages")
async def put_project_messages(project_id: str, body: MessagesBody, request: Request):
    """Persist the chat thread so it survives a reload and follows the user across devices."""
    user_id = await _auth_and_provision(request)
    if not await store.get_project(project_id, user_id):
        raise HTTPException(status_code=404, detail="not found")
    stored = await store.put_messages(project_id, body.messages)
    return {"ok": True, "stored": stored}
