"""create-application-pipeline — MVP vertical slice (phase 14 subset).

Proves the whole spine end-to-end:
  allocate shortid -> ensure gitea account -> generate repo from template -> checkout ->
  render secrets/.env -> normalize+build+deploy skeleton -> confirm public /health live ->
  mark project `live`.

Strategy docs and the multi-feature build loop (phases 13/15/17/18/19) are NOT built here.
Structural work is deterministic; no LLM call is required for the skeleton slice.
"""
import logging
import secrets
from typing import Callable

from server import deployer, gitea, store, workspace
from server.harness import engine
from server.harness.engine import Ctx, Step

logger = logging.getLogger(__name__)


def build_create_steps(user_id: str, email: str | None) -> list[Step]:
    """The ordered steps for the create MVP. project_id + repo info flow through ctx.state."""

    async def s_ensure_gitea(ctx: Ctx):
        acct = await gitea.ensure_user(user_id, email)
        ctx.state["gitea_user"] = acct["gitea_username"]
        ctx.state["gitea_token"] = acct["token"]
        return f"gitea user {acct['gitea_username']}"

    async def s_create_repo(ctx: Ctx):
        info = await gitea.create_project_repo(ctx.state["gitea_user"], ctx.project_id)
        ctx.state["repo"] = info["repo"]
        ctx.emit({"type": "repo", "full_name": info["full_name"]})
        return f"repo {info['full_name']}"

    async def s_checkout(ctx: Ctx):
        ws = await workspace.checkout(
            ctx.project_id, ctx.state["gitea_user"], ctx.state["repo"],
            ctx.state["gitea_token"], author_name=ctx.state["gitea_user"],
            author_email=email or f"{ctx.state['gitea_user']}@builderapps.osmike.com",
        )
        ctx.state["workspace"] = str(ws)
        return f"checked out to {ws}"

    async def s_secrets(ctx: Ctx):
        db_password = secrets.token_urlsafe(18)
        app_secret = secrets.token_urlsafe(24)
        await store.put_secret(ctx.project_id, "db_password", db_password)
        await store.put_secret(ctx.project_id, "app_secret", app_secret)
        ctx.state["db_password"] = db_password
        ctx.state["app_secret"] = app_secret
        return "secrets allocated"

    async def s_deploy(ctx: Ctx):
        await store.set_project_status(ctx.project_id, "deploying")
        ctx.emit({"type": "progress", "stage": "deploy",
                  "detail": "building + starting the skeleton stack"})
        from pathlib import Path
        proj = await store.get_project(ctx.project_id)
        res = await deployer.deploy(
            ctx.project_id, Path(ctx.state["workspace"]),
            db_password=ctx.state["db_password"], app_secret=ctx.state["app_secret"],
            title=(proj or {}).get("title", ""),
        )
        ctx.state["public_health"] = res["public_health"]
        ctx.emit({"type": "deploy", "url": f"https://{ctx.project_id}.{deployer.SITES_BASE}/",
                  "health": res["public_health"]})
        return f"live: {res['public_health']}"

    async def s_commit(ctx: Ctx):
        # commit the .normalized compose + any workspace changes (skeleton is unchanged
        # from template, so this may be a no-op — that's fine).
        changed = await workspace.commit_push(
            ctx.project_id, "builderapps: skeleton deployed",
            ctx.state["gitea_user"], ctx.state["repo"], ctx.state["gitea_token"],
        )
        return "pushed" if changed else "nothing to commit"

    async def s_finalize(ctx: Ctx):
        await store.set_project_status(ctx.project_id, "live")
        return "project live"

    return [
        ("ensure_gitea_account", s_ensure_gitea),
        ("create_repo", s_create_repo),
        ("checkout", s_checkout),
        ("allocate_secrets", s_secrets),
        ("deploy_skeleton", s_deploy),
        ("commit_push", s_commit),
        ("finalize", s_finalize),
    ]


async def run_create(project_id: str, run_id: int, user_id: str, email: str | None,
                     emit: Callable[[dict], None]) -> dict:
    steps = build_create_steps(user_id, email)
    lk = workspace.lock(project_id)
    async with lk:
        try:
            return await engine.run(project_id, run_id, steps, emit)
        except Exception:
            await store.set_project_status(project_id, "failed")
            raise
