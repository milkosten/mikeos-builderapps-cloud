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

from server import db, deployer, gitea, introspect, naming, runner, store, workspace, usage
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
    usage.install()          # token/cost accounting sink (best-effort, never blocks a build)
    logger.info("builderapps-cloud up (%s); public=%s sites=%s",
                runner.INSTANCE_ID, PUBLIC_BASE, SITES_BASE)
    # RECOVERY: a control-plane redeploy kills every in-flight pipeline. Take over anything
    # left `running` by the previous process and resume it (or fail it with a reason) — a run
    # is never left in limbo. Then keep a janitor sweeping for the same condition.
    try:
        recovered = await runner.sweep_on_boot()
        if recovered:
            logger.warning("boot sweep recovered %d interrupted run(s)", recovered)
    except Exception:  # noqa: BLE001 — never block startup on recovery
        logger.exception("boot sweep failed")
    runner.start_janitor()
    yield
    await runner.shutdown()
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


class LifecycleBody(BaseModel):
    action: str = Field(..., min_length=1, max_length=16)


# ---- SSE plumbing ---------------------------------------------------------
def _sse(item: dict) -> str:
    return f"data: {json.dumps(item)}\n\n"


def _observe(run_id: int, head: Optional[dict] = None) -> StreamingResponse:
    """Stream a background run's events to this client.

    The client is a pure OBSERVER: it subscribes to the run's event broker and, when it goes
    away, we merely unsubscribe. The pipeline itself is an independent task owned by
    `server.runner`, so closing the tab (or losing the connection) can no longer kill a build
    — which is precisely what used to orphan runs.
    """
    async def event_gen():
        q, replay = runner.subscribe(run_id)
        try:
            if head:
                yield _sse(head)
            for ev in replay:                       # catch a reconnecting client up
                if ev.get("type") != "eof":
                    yield _sse(ev)
            if not runner.is_active(run_id):
                return
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    # SSE comment keep-alive: proves the connection is healthy through Caddy
                    # without inventing an event the SPA would have to understand.
                    yield ": keepalive\n\n"
                    if not runner.is_active(run_id):
                        return
                    continue
                if item.get("type") == "eof":
                    return
                yield _sse(item)
        finally:
            runner.unsubscribe(run_id, q)

    return StreamingResponse(
        event_gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"})


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
    steps as SSE. The project + repo binding are created up-front; the pipeline then runs as a
    DURABLE BACKGROUND TASK (server.runner) and this response merely observes it, so the build
    survives both a client disconnect and a control-plane redeploy."""
    user_id = await _auth_and_provision(request)
    email = getattr(request.state, "user_email", None)

    shortid = await store.alloc_shortid()
    gitea_user = gitea.gitea_username_for(user_id)
    repo_name = f"app-{shortid}"
    # A real, short product name instead of a truncated prompt. Generated HERE (not later in
    # the pipeline) so the topbar dropdown and the Apps list read well from the first render.
    # Bounded + best-effort: a naming failure degrades to a deterministic slug, never a 500.
    usage.set_context(shortid, None, "name")     # attribute the naming tokens to this project
    title = naming.clean(body.title) or await naming.name_for(body.prompt)

    await store.create_project(
        id=shortid, user_id=user_id, gitea_owner=gitea_user, gitea_repo=repo_name,
        subdomain=f"{shortid}.{SITES_BASE}", title=title, prompt=body.prompt,
        status="creating", pipeline="create",
    )
    run_id = await store.create_run(shortid, "create", body.prompt, total_steps=7)

    emit = runner.emitter(run_id)
    await runner.start(
        run_id, shortid,
        lambda: pipeline.run_create(shortid, run_id, user_id, email, emit))

    # initial event so the client immediately learns the id + url
    return _observe(run_id, head={"type": "created", "id": shortid,
                                  "url": f"https://{shortid}.{SITES_BASE}/",
                                  "run_id": run_id})


# ---- update project (phase 18) --------------------------------------------
@app.post("/api/projects/{project_id}/update")
async def update_project(project_id: str, body: UpdateBody, request: Request):
    """Apply a natural-language change to an existing app (ownership-checked). Streams the
    update run as SSE, same vocabulary as create. As with create, the run itself is a durable
    background task — this response only observes it."""
    user_id = await _auth_and_provision(request)
    email = getattr(request.state, "user_email", None)

    proj = await store.get_project(project_id, user_id)   # ownership-checked
    if not proj:
        raise HTTPException(status_code=404, detail="not found")

    await store.set_project_status(project_id, "building")
    run_id = await store.create_run(project_id, "update", body.request, total_steps=5)

    emit = runner.emitter(run_id)
    await runner.start(
        run_id, project_id,
        lambda: pipeline.run_update(project_id, run_id, user_id, email, body.request, emit))

    return _observe(run_id, head={"type": "created", "id": project_id,
                                  "url": f"https://{project_id}.{SITES_BASE}/",
                                  "run_id": run_id})


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


@app.get("/api/projects/{project_id}/events")
async def project_events(project_id: str, request: Request):
    """RE-ATTACH to the live run's SSE stream after a reload/disconnect.

    Because the run outlives the request that started it, a client that dropped can come back
    and pick the stream up (with the events it missed replayed first). Returns immediately
    when the run is already finished — the durable record is `/steps`.
    """
    user_id = await _auth_and_provision(request)
    if not await store.get_project(project_id, user_id):
        raise HTTPException(status_code=404, detail="not found")
    latest = await store.steps_for_latest_run(project_id)
    run_id = latest.get("run_id")
    if not run_id:
        raise HTTPException(status_code=404, detail="no run for this project")
    return _observe(int(run_id), head={"type": "attached", "id": project_id,
                                       "run_id": int(run_id)})


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


# ===========================================================================
# Project introspection — the builder UI's tabs (Goals/Code/Database/…)
#
# Every route below is dual-authed AND ownership-checked through `_owned()`, which is also
# where the project id is validated against ^[0-9a-z]{6}$ *before* it can reach a docker or
# psql argv. Another user's project is indistinguishable from a missing one: 404.
# Each list endpoint returns an EMPTY list rather than 404/500 when the project simply has
# no data yet — the pipeline may still be running.
# ===========================================================================
async def _owned(project_id: str, request: Request) -> dict:
    """Authenticate, validate the id shape, and confirm the caller owns the project."""
    user_id = await _auth_and_provision(request)
    try:
        introspect.assert_shortid(project_id)
    except introspect.BadPath:
        raise HTTPException(status_code=404, detail="not found")
    proj = await store.get_project(project_id, user_id)
    if not proj:
        raise HTTPException(status_code=404, detail="not found")
    return proj


def _bad_path(e: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e) or "invalid path")


# ---- 1/2 docs -------------------------------------------------------------
@app.get("/api/projects/{project_id}/usage")
async def project_usage(project_id: str, request: Request):
    """Token + cost accounting for the builder's Usage tab."""
    await _owned(project_id, request)
    return await store.usage_for_project(project_id)


@app.get("/api/projects/{project_id}/docs")
async def project_docs(project_id: str, request: Request):
    await _owned(project_id, request)
    return {"docs": introspect.list_docs(project_id)}


@app.get("/api/projects/{project_id}/docs/{name}")
async def project_doc(project_id: str, name: str, request: Request):
    await _owned(project_id, request)
    try:
        doc = introspect.read_doc(project_id, name)
    except introspect.BadPath as e:
        raise _bad_path(e)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    return doc


# ---- 3/4 repo tree + file content -----------------------------------------
@app.get("/api/projects/{project_id}/files")
async def project_files(project_id: str, request: Request, path: str = ""):
    await _owned(project_id, request)
    try:
        return {"entries": introspect.list_files(project_id, path)}
    except introspect.BadPath as e:
        raise _bad_path(e)


@app.get("/api/projects/{project_id}/file")
async def project_file(project_id: str, request: Request, path: str = ""):
    await _owned(project_id, request)
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    try:
        f = introspect.read_file(project_id, path)
    except introspect.BadPath as e:
        raise _bad_path(e)
    if f is None:
        raise HTTPException(status_code=404, detail="file not found")
    return f


# ---- 5 the project's own database -----------------------------------------
@app.get("/api/projects/{project_id}/database")
async def project_database(project_id: str, request: Request):
    await _owned(project_id, request)
    db_password = await store.get_secret(project_id, "db_password")
    return await introspect.database(project_id, db_password)


# ---- 6 secrets ------------------------------------------------------------
@app.get("/api/projects/{project_id}/secrets")
async def project_secrets(project_id: str, request: Request, reveal: int = 0):
    """Masked by default. `?reveal=1` returns the real values — only ever to the owner, who
    is the only caller that gets this far, and the values are never logged."""
    await _owned(project_id, request)
    items = await store.list_secrets(project_id)
    return {"secrets": [
        {"key": s["key"], "masked": introspect.mask_secret(s["value"]),
         "value": s["value"] if reveal else None}
        for s in items
    ]}


# ---- 7 logs ---------------------------------------------------------------
@app.get("/api/projects/{project_id}/logs")
async def project_logs(project_id: str, request: Request, tail: int = 200):
    await _owned(project_id, request)
    return {"lines": await introspect.logs(project_id, tail)}


# ---- 8 commits ------------------------------------------------------------
@app.get("/api/projects/{project_id}/commits")
async def project_commits(project_id: str, request: Request, limit: int = 30):
    proj = await _owned(project_id, request)
    return {"commits": await introspect.commits(
        project_id, proj.get("gitea_owner", ""), proj.get("gitea_repo", ""), limit)}


# ---- 9 deployments --------------------------------------------------------
@app.get("/api/projects/{project_id}/deployments")
async def project_deployments(project_id: str, request: Request):
    await _owned(project_id, request)
    rows = await store.list_deployments(project_id)
    return {"deployments": [
        {"id": r["id"], "image_tag": r["image_tag"], "status": r["status"],
         "health": r["health"], "started_at": r["started_at"],
         "finished_at": r["finished_at"]}
        for r in rows
    ]}


# ---- 10 runtime QA --------------------------------------------------------
@app.get("/api/projects/{project_id}/qa")
async def project_qa(project_id: str, request: Request):
    await _owned(project_id, request)
    thread = await store.get_raw_thread(project_id)
    qa_entries = [t for t in thread if str(t.get("role", "")).lower() == "qa"]
    steps = await store.all_steps_for_project(project_id)
    qa_steps = [s for s in steps if s.get("name") == "runtime_qa"]
    return {"rounds": introspect.qa_rounds(qa_entries, qa_steps)}


# ---- 11 backlog -----------------------------------------------------------
@app.get("/api/projects/{project_id}/backlog")
async def project_backlog(project_id: str, request: Request):
    await _owned(project_id, request)
    steps = await store.all_steps_for_project(project_id)
    return {"items": introspect.backlog(project_id, steps)}


# ---- 12 routes ------------------------------------------------------------
@app.get("/api/projects/{project_id}/routes")
async def project_routes(project_id: str, request: Request):
    await _owned(project_id, request)
    return {"routes": introspect.routes(project_id)}


# ---- 13 metrics -----------------------------------------------------------
@app.get("/api/projects/{project_id}/metrics")
async def project_metrics(project_id: str, request: Request):
    await _owned(project_id, request)
    return {"containers": await introspect.metrics(project_id)}


# ---- 14 cache -------------------------------------------------------------
@app.get("/api/projects/{project_id}/cache")
async def project_cache(project_id: str, request: Request):
    await _owned(project_id, request)
    return await introspect.cache(project_id)


# ---- 15 domain + TLS ------------------------------------------------------
@app.get("/api/projects/{project_id}/domain")
async def project_domain(project_id: str, request: Request):
    proj = await _owned(project_id, request)
    return await introspect.domain(project_id, proj.get("subdomain", ""))


# ---- 16 non-secret env ----------------------------------------------------
@app.get("/api/projects/{project_id}/env")
async def project_env(project_id: str, request: Request):
    await _owned(project_id, request)
    return {"env": await introspect.env(project_id)}


# ---- 17 lifecycle ---------------------------------------------------------
@app.post("/api/projects/{project_id}/lifecycle")
async def project_lifecycle(project_id: str, body: LifecycleBody, request: Request):
    """stop | start | restart | destroy. `destroy` also marks the project row and reaps the
    workspace checkout and the data volumes. The returned status is OBSERVED from docker,
    not assumed."""
    await _owned(project_id, request)
    action = (body.action or "").strip().lower()
    if action not in introspect.LIFECYCLE_ACTIONS:
        raise HTTPException(status_code=400, detail="action must be one of "
                            + "|".join(introspect.LIFECYCLE_ACTIONS))
    try:
        if action == "stop":
            await deployer.stop(project_id)
            await store.set_project_status(project_id, "stopped")
        elif action == "start":
            await deployer.start(project_id)
            await store.set_project_status(project_id, "live")
        elif action == "restart":
            await deployer.restart(project_id)
            await store.set_project_status(project_id, "live")
        else:  # destroy
            await deployer.destroy(project_id)
            await introspect.reap_volumes(project_id)
            workspace.cleanup(project_id)
            await store.set_project_status(project_id, "destroyed")
    except Exception as e:  # noqa: BLE001
        logger.exception("lifecycle %s failed for %s", action, project_id)
        raise HTTPException(status_code=500, detail=f"{action} failed: {e}")
    return {"ok": True, "status": await introspect.observed_status(project_id)}
