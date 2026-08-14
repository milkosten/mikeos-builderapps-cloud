"""create + update application pipelines (phases 13/14/15/16/17/18).

create-application-pipeline (the 20-30+ step run):
  ensure gitea -> repo from template -> checkout -> secrets -> deploy skeleton (live in ~1min)
  -> strategy docs (phase 13) -> parse backlog -> data layer (phase 15) -> per-feature build loop
  (phase 15) -> final redeploy (phase 16) -> runtime QA + autofix (phase 17) -> finalize.

update-application-pipeline (phase 18):
  checkout latest -> context block (brief + TECHNICAL-PLAN + last N commits) -> minimal-diff
  codegen -> apply -> deploy (health-gate, last-good rollback) -> runtime QA on the change ->
  commit + push.

Structural work (repo/compose/deploy/git) stays deterministic. The LLM (Kimi) decides *what the
app is* and writes *app code*. The SSE vocabulary the SPA consumes is preserved; new steps add
more step_start/step_done pairs plus `commit{...}` and `qa{...}` events.
"""
import logging
import secrets
from pathlib import Path
from typing import Callable, Optional

from server import deployer, gitea, store, workspace
from server.harness import backlog as backlog_mod
from server.harness import codegen, engine, runtime_qa
from server.harness.engine import Ctx, Step

logger = logging.getLogger(__name__)

# The build loop is bounded so it can never run away (designer lesson: hard-cap steps).
_MAX_FEATURES = 12


# --------------------------------------------------------------------------
# shared deterministic steps
# --------------------------------------------------------------------------
def _s_ensure_gitea(user_id: str, email: Optional[str]):
    async def step(ctx: Ctx):
        acct = await gitea.ensure_user(user_id, email)
        ctx.state["gitea_user"] = acct["gitea_username"]
        ctx.state["gitea_token"] = acct["token"]
        return f"gitea user {acct['gitea_username']}"
    return step


def _s_create_repo():
    async def step(ctx: Ctx):
        info = await gitea.create_project_repo(ctx.state["gitea_user"], ctx.project_id)
        ctx.state["repo"] = info["repo"]
        ctx.emit({"type": "repo", "full_name": info["full_name"]})
        return f"repo {info['full_name']}"
    return step


def _s_checkout(email: Optional[str]):
    async def step(ctx: Ctx):
        ws = await workspace.checkout(
            ctx.project_id, ctx.state["gitea_user"], ctx.state["repo"],
            ctx.state["gitea_token"], author_name=ctx.state["gitea_user"],
            author_email=email or f"{ctx.state['gitea_user']}@builderapps.osmike.com",
        )
        ctx.state["workspace"] = str(ws)
        return f"checked out to {ws}"
    return step


async def _load_secrets_into_state(ctx: Ctx) -> None:
    """Ensure db_password/app_secret are in ctx.state (create sets them; update reloads them)."""
    if ctx.state.get("db_password") and ctx.state.get("app_secret"):
        return
    dbp = await store.get_secret(ctx.project_id, "db_password") or secrets.token_urlsafe(18)
    aps = await store.get_secret(ctx.project_id, "app_secret") or secrets.token_urlsafe(24)
    ctx.state["db_password"] = dbp
    ctx.state["app_secret"] = aps


def _s_secrets():
    async def step(ctx: Ctx):
        db_password = secrets.token_urlsafe(18)
        app_secret = secrets.token_urlsafe(24)
        await store.put_secret(ctx.project_id, "db_password", db_password)
        await store.put_secret(ctx.project_id, "app_secret", app_secret)
        ctx.state["db_password"] = db_password
        ctx.state["app_secret"] = app_secret
        return "secrets allocated"
    return step


def _s_deploy(stage_detail: str, commit_status: str):
    async def step(ctx: Ctx):
        await _load_secrets_into_state(ctx)
        await store.set_project_status(ctx.project_id, commit_status)
        ctx.emit({"type": "progress", "stage": "deploy", "detail": stage_detail})
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
    return step


async def _commit(ctx: Ctx, message: str) -> bool:
    changed = await workspace.commit_push(
        ctx.project_id, message,
        ctx.state["gitea_user"], ctx.state["repo"], ctx.state["gitea_token"],
    )
    if changed:
        ctx.emit({"type": "commit", "message": message})
    return changed


# --------------------------------------------------------------------------
# phase 13 — strategy artifacts + backlog parse
# --------------------------------------------------------------------------
def _s_strategy(brief: str):
    async def step(ctx: Ctx):
        proj = await store.get_project(ctx.project_id)
        title = (proj or {}).get("title") or brief[:60]
        ctx.emit({"type": "progress", "stage": "strategy",
                  "detail": "writing vision / ICP / UX / persona / marketing / technical plan"})
        docs = await codegen.write_strategy_docs(brief, title)
        for relpath, content in docs.items():
            workspace.write_file(ctx.project_id, relpath, content)
        ctx.state["tech_plan"] = docs.get("docs/TECHNICAL-PLAN.md", "")
        await _commit(ctx, f"docs: strategy artifacts for {title}")
        return f"{len(docs)} strategy docs written"
    return step


def _s_parse_backlog():
    async def step(ctx: Ctx):
        tp = ctx.state.get("tech_plan") or (
            workspace.read_file_capped(ctx.project_id, "docs/TECHNICAL-PLAN.md") or "")
        ctx.state["tech_plan"] = tp
        items = backlog_mod.parse_backlog(tp, cap=_MAX_FEATURES)
        if not items:
            # never leave the loop empty — fall back to one holistic build task
            items = ["Implement the full app described in the brief and technical plan: "
                     "all routes, all pages, wired end-to-end."]
        ctx.state["backlog"] = items
        ctx.state["changes"] = []
        return f"backlog: {len(items)} features -> {items}"
    return step


# --------------------------------------------------------------------------
# phase 15 — data layer + build loop
# --------------------------------------------------------------------------
def _next_migration_name(shortid: str, slug: str) -> str:
    import os
    d = workspace.path_for(shortid) / "migrations"
    d.mkdir(parents=True, exist_ok=True)
    nums = [int(m.group(1)) for f in os.listdir(d)
            if (m := __import__("re").match(r"(\d+)_", f))]
    nxt = (max(nums) + 1) if nums else 1
    slug = __import__("re").sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")[:40] or "feature"
    return f"{nxt:03d}_{slug}.sql"


def _s_data_layer(brief: str):
    async def step(ctx: Ctx):
        ctx.emit({"type": "progress", "stage": "data_layer",
                  "detail": "designing schema + migration"})
        design = await codegen.design_schema(brief, ctx.state.get("tech_plan", ""))
        name = _next_migration_name(ctx.project_id, "app_schema")
        workspace.write_file(ctx.project_id, f"migrations/{name}", design["sql"].rstrip() + "\n")
        ctx.state.setdefault("changes", []).append(f"schema: {design['summary']}")
        await _commit(ctx, f"feat: data model — {design['summary']}")
        return f"migration migrations/{name}: {design['summary']}"
    return step


# The set of files the model is allowed to see/edit each feature (small context block).
_BUILD_CONTEXT_FILES = ["server.js", "public/index.html"]


def _build_context_files(shortid: str) -> dict:
    files = {}
    for rel in _BUILD_CONTEXT_FILES:
        c = workspace.read_file_capped(shortid, rel)
        if c is not None:
            files[rel] = c
    return files


def _s_feature(idx: int, feature: str, brief: str):
    async def step(ctx: Ctx):
        ctx.emit({"type": "progress", "stage": "build_feature",
                  "detail": f"[{idx+1}] {feature[:120]}"})
        current = _build_context_files(ctx.project_id)
        files = await codegen.generate_files(
            brief=brief, tech_plan=ctx.state.get("tech_plan", ""),
            feature=feature, current_files=current,
            recent_changes=ctx.state.get("changes", []),
        )
        wrote = []
        for path, content in files.items():
            # migrations get a proper NNN name if the model didn't number them well
            if path.startswith("migrations/") and not __import__("re").match(
                    r"migrations/\d{3}_", path):
                path = "migrations/" + _next_migration_name(ctx.project_id, feature)
            workspace.write_file(ctx.project_id, path, content)
            wrote.append(path)
        # rebuild + restart just the app, health-gate (deployer does the full compose)
        await _load_secrets_into_state(ctx)
        proj = await store.get_project(ctx.project_id)
        try:
            await deployer.deploy(
                ctx.project_id, workspace.path_for(ctx.project_id),
                db_password=ctx.state["db_password"], app_secret=ctx.state["app_secret"],
                title=(proj or {}).get("title", ""))
        except Exception as e:  # noqa: BLE001 — feed the error back once, then continue
            logger.warning("feature %s build failed, one retry: %s", feature, e)
            fix = await codegen.generate_files(
                brief=brief, tech_plan=ctx.state.get("tech_plan", ""),
                feature=f"The previous change broke the build/health. Error:\n{str(e)[:1500]}\n"
                        f"Fix it minimally for feature: {feature}",
                current_files=_build_context_files(ctx.project_id),
                recent_changes=ctx.state.get("changes", []), minimal=True)
            for path, content in fix.items():
                workspace.write_file(ctx.project_id, path, content)
                if path not in wrote:
                    wrote.append(path)
            await deployer.deploy(
                ctx.project_id, workspace.path_for(ctx.project_id),
                db_password=ctx.state["db_password"], app_secret=ctx.state["app_secret"],
                title=(proj or {}).get("title", ""))
        ctx.state.setdefault("changes", []).append(f"{feature} ({', '.join(wrote)})")
        await _commit(ctx, f"feat: {feature[:70]}")
        return f"built {feature[:60]} -> {wrote}"
    return step


# --------------------------------------------------------------------------
# phase 17 — runtime QA
# --------------------------------------------------------------------------
def _s_qa(brief: str):
    async def step(ctx: Ctx):
        await _load_secrets_into_state(ctx)
        proj = await store.get_project(ctx.project_id)
        url = f"https://{ctx.project_id}.{deployer.SITES_BASE}/"

        async def commit_fix(msg: str):
            await _commit(ctx, msg)

        report = await runtime_qa.run_qa(
            ctx.project_id, brief=brief, tech_plan=ctx.state.get("tech_plan", ""),
            url=url, db_password=ctx.state["db_password"],
            app_secret=ctx.state["app_secret"], title=(proj or {}).get("title", ""),
            emit=ctx.emit, max_rounds=2, commit_fix=commit_fix,
        )
        ctx.emit({"type": "qa", "final": True, "clean": report["clean"],
                  "rounds": report["rounds"], "critic": report["critic"],
                  "errors": report["errors"][:10], "network": report["network"][:10],
                  "fixes": report["fixes"]})
        await store.append_message(ctx.project_id, "qa", report.get("critic", ""),
                                   {"clean": report["clean"], "rounds": report["rounds"],
                                    "errors": report["errors"][:20],
                                    "network": report["network"][:20],
                                    # the QA tab reads server_errors — persist them properly
                                    # instead of leaving it to infer from `network`.
                                    "server_errors": (report.get("server_errs") or "")
                                    .splitlines()[:20]})
        await _commit(ctx, "qa: runtime QA results")
        status = "clean" if report["clean"] else "live-with-warnings"
        return f"QA {status} after {report['rounds']} round(s); critic: {report['critic'][:200]}"
    return step


def _s_finalize():
    async def step(ctx: Ctx):
        await store.set_project_status(ctx.project_id, "live")
        return "project live"
    return step


# --------------------------------------------------------------------------
# create-pipeline assembly (two-pass: spine+strategy -> derive backlog -> tail)
# --------------------------------------------------------------------------
async def run_create(project_id: str, run_id: int, user_id: str, email: Optional[str],
                     emit: Callable[[dict], None]) -> dict:
    """Run the full create pipeline. The backlog is derived from the strategy docs during the
    run; because the step list must be known to the engine up front (for stable idx), we build
    the strategy+backlog first (deterministically resumable), then assemble the full step list."""
    proj = await store.get_project(project_id)
    brief = (proj or {}).get("prompt", "") or ""

    lk = workspace.lock(project_id)
    async with lk:
        try:
            # Prelude: run the spine + strategy so we can compute the backlog and the full step
            # list. We do this via a small first engine pass, then assemble the real run.
            prelude_steps: list[Step] = [
                ("ensure_gitea_account", _s_ensure_gitea(user_id, email)),
                ("create_repo", _s_create_repo()),
                ("checkout", _s_checkout(email)),
                ("allocate_secrets", _s_secrets()),
                ("deploy_skeleton", _s_deploy("building + starting the skeleton stack", "deploying")),
                ("strategy_artifacts", _s_strategy(brief)),
                ("parse_backlog", _s_parse_backlog()),
            ]
            state = await engine.run_partial(project_id, run_id, prelude_steps, emit,
                                             base_idx=0)
            backlog_items = state.get("backlog", [])

            # Assemble the remainder (data layer + features + deploy + QA + finalize) and run,
            # continuing the same run_id at the next idx.
            tail_steps: list[Step] = [("data_layer", _s_data_layer(brief))]
            for i, feat in enumerate(backlog_items):
                tail_steps.append((f"build_{i+1:02d}", _s_feature(i, feat, brief)))
            tail_steps.append(("final_deploy", _s_deploy("redeploying the finished stack", "deploying")))
            tail_steps.append(("runtime_qa", _s_qa(brief)))
            tail_steps.append(("finalize", _s_finalize()))

            total = len(prelude_steps) + len(tail_steps)
            await store.set_run_total(run_id, total)
            state = await engine.run_partial(project_id, run_id, tail_steps, emit,
                                             base_idx=len(prelude_steps), state=state,
                                             finish=True, total=total)
            return state
        except Exception:
            await store.set_project_status(project_id, "failed")
            raise


# --------------------------------------------------------------------------
# update-pipeline (phase 18)
# --------------------------------------------------------------------------
def build_update_steps(user_id: str, email: Optional[str], request_text: str) -> list[Step]:
    async def s_context(ctx: Ctx):
        # checkout latest + load brief/plan/last-commits context block
        acct = await gitea.ensure_user(user_id, email)
        ctx.state["gitea_user"] = acct["gitea_username"]
        ctx.state["gitea_token"] = acct["token"]
        proj = await store.get_project(ctx.project_id)
        ctx.state["repo"] = (proj or {}).get("gitea_repo") or f"app-{ctx.project_id}"
        await workspace.checkout(ctx.project_id, ctx.state["gitea_user"], ctx.state["repo"],
                                 ctx.state["gitea_token"])
        ctx.state["workspace"] = str(workspace.path_for(ctx.project_id))
        ctx.state["brief"] = (proj or {}).get("prompt", "") or ""
        ctx.state["tech_plan"] = workspace.read_file_capped(
            ctx.project_id, "docs/TECHNICAL-PLAN.md") or ""
        ctx.state["recent_commits"] = await workspace.recent_commits(ctx.project_id, n=8)
        return f"context ready ({len(ctx.state['recent_commits'])} recent commits)"

    async def s_plan_and_apply(ctx: Ctx):
        ctx.emit({"type": "progress", "stage": "update_plan",
                  "detail": f"planning minimal diff: {request_text[:100]}"})
        current = _build_context_files(ctx.project_id)
        files = await codegen.generate_files(
            brief=ctx.state["brief"], tech_plan=ctx.state["tech_plan"],
            feature=f"CHANGE REQUEST: {request_text}",
            current_files=current,
            recent_changes=ctx.state.get("recent_commits", []),
            minimal=True,
        )
        wrote = []
        for path, content in files.items():
            if path.startswith("migrations/") and not __import__("re").match(
                    r"migrations/\d{3}_", path):
                path = "migrations/" + _next_migration_name(ctx.project_id, request_text)
            workspace.write_file(ctx.project_id, path, content)
            wrote.append(path)
        ctx.state["changed_files"] = wrote
        return f"applied minimal diff -> {wrote}"

    async def s_deploy(ctx: Ctx):
        # keep last-good compose for rollback
        await _load_secrets_into_state(ctx)
        proj = await store.get_project(ctx.project_id)
        ws = workspace.path_for(ctx.project_id)
        norm = ws / "docker-compose.normalized.yml"
        last_good = norm.read_text("utf-8") if norm.exists() else None
        ctx.emit({"type": "progress", "stage": "deploy", "detail": "redeploying with the change"})
        try:
            res = await deployer.deploy(
                ctx.project_id, ws, db_password=ctx.state["db_password"],
                app_secret=ctx.state["app_secret"], title=(proj or {}).get("title", ""))
        except Exception as e:  # noqa: BLE001 — rollback the source to last commit + redeploy
            logger.warning("update deploy failed for %s, rolling back: %s", ctx.project_id, e)
            await workspace.checkout(ctx.project_id, ctx.state["gitea_user"],
                                     ctx.state["repo"], ctx.state["gitea_token"])
            if last_good:
                norm.write_text(last_good, "utf-8")
            await deployer.deploy(
                ctx.project_id, ws, db_password=ctx.state["db_password"],
                app_secret=ctx.state["app_secret"], title=(proj or {}).get("title", ""))
            raise RuntimeError(f"update reverted (deploy failed): {e}")
        ctx.state["public_health"] = res["public_health"]
        ctx.emit({"type": "deploy", "url": f"https://{ctx.project_id}.{deployer.SITES_BASE}/",
                  "health": res["public_health"]})
        return f"live: {res['public_health']}"

    async def s_qa(ctx: Ctx):
        proj = await store.get_project(ctx.project_id)
        url = f"https://{ctx.project_id}.{deployer.SITES_BASE}/"

        async def commit_fix(msg: str):
            await _commit(ctx, msg)

        report = await runtime_qa.run_qa(
            ctx.project_id, brief=ctx.state["brief"], tech_plan=ctx.state["tech_plan"],
            url=url, db_password=ctx.state["db_password"], app_secret=ctx.state["app_secret"],
            title=(proj or {}).get("title", ""), emit=ctx.emit, max_rounds=2,
            commit_fix=commit_fix)
        ctx.emit({"type": "qa", "final": True, "clean": report["clean"],
                  "rounds": report["rounds"], "critic": report["critic"],
                  "errors": report["errors"][:10], "network": report["network"][:10]})
        return f"QA {'clean' if report['clean'] else 'live-with-warnings'}: {report['critic'][:150]}"

    async def s_commit(ctx: Ctx):
        changed = await _commit(ctx, f"update: {request_text[:70]}")
        await store.append_message(ctx.project_id, "update", request_text,
                                   {"files": ctx.state.get("changed_files", [])})
        await store.set_project_status(ctx.project_id, "live")
        return "pushed" if changed else "nothing to commit"

    return [
        ("update_context", s_context),
        ("plan_and_apply", s_plan_and_apply),
        ("deploy", s_deploy),
        ("runtime_qa", s_qa),
        ("commit_push", s_commit),
    ]


async def run_update(project_id: str, run_id: int, user_id: str, email: Optional[str],
                     request_text: str, emit: Callable[[dict], None]) -> dict:
    steps = build_update_steps(user_id, email, request_text)
    lk = workspace.lock(project_id)
    async with lk:
        try:
            return await engine.run(project_id, run_id, steps, emit)
        except Exception:
            await store.set_project_status(project_id, "failed")
            raise
