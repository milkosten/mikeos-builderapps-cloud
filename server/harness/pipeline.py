"""create + update application pipelines (phases 13/14/15/16/17/18).

create-application-pipeline (the 20-30+ step run):
  ensure gitea -> repo from template -> checkout -> secrets -> deploy skeleton (live in ~1min)
  -> strategy docs (phase 13) -> parse backlog -> data layer (phase 15) -> per-feature build loop
  (phase 15) -> final redeploy (phase 16) -> runtime QA + autofix (phase 17) -> finalize.

update-application-pipeline (phase 18, on the tool loop since phase 26/28):
  checkout latest -> context (brief + TECHNICAL-PLAN + last N commits + the file list) ->
  AGENTIC minimal diff (read/grep/edit, syntax-gated, full transcript on disk) -> deploy
  (health-gate; repair pass first, last-good rollback only if that fails) -> runtime QA on the
  change -> commit + push.

  A change request is the "read before you write" case par excellence: the `{"links"}` ->
  `{"items"}` regression happened because a whole-file rewrite renamed a server response key
  while fixing the client. The agent must now open the endpoint before it touches the caller.

Structural work (repo/compose/deploy/git) stays deterministic. The LLM (Kimi) decides *what the
app is* and writes *app code*. The SSE vocabulary the SPA consumes is preserved; new steps add
more step_start/step_done pairs plus `commit{...}` and `qa{...}` events.
"""
import logging
import os
import secrets
from pathlib import Path
from typing import Callable, Optional

from server import deployer, gitea, store, workspace
from server.harness import backlog as backlog_mod
from server.harness import agentic, codegen, engine, runtime_qa
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
        ctx.state["feature_total"] = len(items)   # denominator of the honest "N of M" summary
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


# Fault injection for verifying graceful degradation on a REAL build (phase 28). Off unless
# BUILDERAPPS_FAULT_FEATURE is set, and it must name BOTH the project and the feature
# ("<shortid>:<n>"), so it can never fire on someone else's build. It fails the health gate,
# which is exactly what a genuinely broken feature does.
def _fault_spec() -> str:
    """The env var, or a `fault.txt` beside the artifacts (so a measurement run can arm the
    fault AFTER the shortid has been allocated, without restarting the control plane)."""
    spec = os.environ.get("BUILDERAPPS_FAULT_FEATURE", "").strip()
    if spec:
        return spec
    try:
        p = Path(os.environ.get("ARTIFACTS_ROOT", "/opt/builderapps/artifacts")) / "fault.txt"
        return p.read_text("utf-8").strip() if p.is_file() else ""
    except Exception:  # noqa: BLE001
        return ""


def _fault_for(project_id: str, feature_no: int) -> bool:
    spec = _fault_spec()
    if not spec or ":" not in spec:
        return False
    proj, _, num = spec.partition(":")
    return proj == project_id and num.strip() == str(feature_no)


async def _redeploy(ctx: Ctx, feature_no: int = 0) -> None:
    """Rebuild + restart the app stack and run the health gate."""
    if feature_no and _fault_for(ctx.project_id, feature_no):
        raise RuntimeError("INJECTED FAULT: health gate failed for this feature "
                           "(BUILDERAPPS_FAULT_FEATURE)")
    await _load_secrets_into_state(ctx)
    proj = await store.get_project(ctx.project_id)
    await deployer.deploy(
        ctx.project_id, workspace.path_for(ctx.project_id),
        db_password=ctx.state["db_password"], app_secret=ctx.state["app_secret"],
        title=(proj or {}).get("title", ""))


async def _feature_attempt(ctx: Ctx, idx: int, feature: str, brief: str, *,
                           attempt: int, prior_error: str = "") -> dict:
    """One full attempt at a feature: agentic edit -> deploy+health-gate -> (repair -> deploy).

    Returns {"wrote": [...], "res": <agent result>}. Raises if the app is still not healthy
    after the in-attempt repair pass.
    """
    step_name = f"build_{idx+1:02d}" if attempt == 1 else f"build_{idx+1:02d}_retry"
    extra = ""
    mode = "feature"
    task = feature
    if prior_error:
        mode = "fix"
        task = (f"A previous attempt to build '{feature}' FAILED and its half-finished work is "
                f"still in the tree. Inspect what is actually there, then finish the feature "
                f"correctly — or, if the partial change is the problem, revert it and "
                f"implement the feature differently.")
        extra = (f"Previous attempt's failure:\n{prior_error[:2000]}\n\n"
                 "Start by reading the files it touched and calling app_logs — the container's "
                 "own output usually names the real cause. Do not guess.")
    res = await agentic.run_agent(
        project_id=ctx.project_id, run_id=ctx.run_id, step=step_name,
        brief=brief, tech_plan=ctx.state.get("tech_plan", ""), task=task, extra=extra,
        recent_changes=ctx.state.get("changes", []), mode=mode,
        require_change=(attempt == 1), emit=ctx.emit)
    wrote = list(res["changed"])
    try:
        await _redeploy(ctx, idx + 1)
    except Exception as e:  # noqa: BLE001 — feed the error back once inside this attempt
        logger.warning("feature %s build failed, one repair pass: %s", feature, e)
        ctx.emit({"type": "progress", "stage": "build_feature",
                  "detail": f"[{idx+1}] deploy failed — repair pass"})
        fix = await agentic.run_agent(
            project_id=ctx.project_id, run_id=ctx.run_id, step=f"{step_name}_fix",
            brief=brief, tech_plan=ctx.state.get("tech_plan", ""),
            task=f"The change just made for '{feature}' broke the build or the health "
                 f"gate. Diagnose and fix it with the smallest possible change.",
            extra=f"Deploy/health error:\n{str(e)[:2000]}\n"
                  f"Files just changed: {', '.join(wrote) or '(none)'}\n"
                  "Use app_logs to see what the container itself reported.",
            recent_changes=ctx.state.get("changes", []), mode="fix",
            require_change=False, emit=ctx.emit)
        for path in fix["changed"]:
            if path not in wrote:
                wrote.append(path)
        await _redeploy(ctx, idx + 1)
    return {"wrote": wrote, "res": res}


def _s_feature(idx: int, feature: str, brief: str):
    """Build ONE backlog feature with the agentic loop (phase 26), non-fatally (phase 28).

    The model navigates the repo with tools (list/grep/read/edit) instead of being handed a
    context block and asked to return whole files, so untouched code stays untouched and the
    per-feature cost stops scaling with file size. If the deploy/health-gate then fails, the
    SAME loop gets a repair pass — and it can `app_logs` the container's own crash output,
    which the whole-file path could never see.

    **A feature that still will not build no longer kills the run.** It gets a second full
    attempt with the first failure fed back in (the agent can read its own partial file and
    tail the crash), and if that fails too the tree is reverted to the last good commit, the
    last-good build is put back on the air, and the step is reported as `skipped` with a
    reason. Losing one feature is a bad outcome; losing the other eleven is a catastrophic one.
    """
    async def step(ctx: Ctx):
        ctx.emit({"type": "progress", "stage": "build_feature",
                  "detail": f"[{idx+1}] {feature[:120]}"})
        try:
            out = await _feature_attempt(ctx, idx, feature, brief, attempt=1)
        except Exception as first:  # noqa: BLE001
            logger.warning("feature '%s' attempt 1 failed: %s", feature, first)
            ctx.emit({"type": "progress", "stage": "build_feature",
                      "detail": f"[{idx+1}] failed — retrying once with the error fed back"})
            try:
                out = await _feature_attempt(ctx, idx, feature, brief, attempt=2,
                                             prior_error=str(first))
            except Exception as second:  # noqa: BLE001 — give up on THIS feature only
                logger.warning("feature '%s' attempt 2 failed: %s", feature, second)
                await workspace.revert_uncommitted(ctx.project_id)
                restored = "reverted to last good commit"
                try:
                    await _redeploy(ctx)          # put the working app back on the air
                except Exception as e3:  # noqa: BLE001
                    restored = f"revert redeploy also failed: {str(e3)[:200]}"
                    logger.error("could not restore %s after skipping a feature: %s",
                                 ctx.project_id, e3)
                reason = (f"failed twice ({str(second)[:220]}); {restored}")
                raise engine.StepSkipped(reason, label=feature[:80])
        wrote, res = out["wrote"], out["res"]
        ctx.state.setdefault("changes", []).append(f"{feature} ({', '.join(wrote)})")
        await _commit(ctx, f"feat: {feature[:70]}")
        return (f"built {feature[:60]} -> {wrote or 'no file changes'} "
                f"({res['tool_calls']} tool calls; {res['summary'][:160]})")
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
            emit=ctx.emit, max_rounds=2, commit_fix=commit_fix, run_id=ctx.run_id,
        )
        ctx.emit({"type": "qa", "final": True, "clean": report["clean"],
                  "rounds": report["rounds"], "critic": report["critic"],
                  "errors": report["errors"][:10], "network": report["network"][:10],
                  "semantic": report.get("semantic", [])[:10],
                  "flows_checked": report.get("flows_checked", 0),
                  "flows_passed": report.get("flows_passed", 0),
                  "fixes": report["fixes"]})
        await store.append_message(ctx.project_id, "qa", report.get("critic", ""),
                                   {"clean": report["clean"], "rounds": report["rounds"],
                                    "errors": report["errors"][:20],
                                    "network": report["network"][:20],
                                    # end-to-end flow results: "seeded a record, did it render"
                                    "semantic": report.get("semantic", [])[:20],
                                    "flows_checked": report.get("flows_checked", 0),
                                    "flows_passed": report.get("flows_passed", 0),
                                    # the QA tab reads server_errors — persist them properly
                                    # instead of leaving it to infer from `network`.
                                    "server_errors": (report.get("server_errs") or "")
                                    .splitlines()[:20]})
        await _commit(ctx, "qa: runtime QA results")
        status = "clean" if report["clean"] else "live-with-warnings"
        flows = (f"; flows {report.get('flows_passed', 0)}/{report.get('flows_checked', 0)} "
                 f"rendered" if report.get("flows_checked") else "")
        return (f"QA {status} after {report['rounds']} round(s){flows}; "
                f"critic: {report['critic'][:200]}")
    return step


def _s_finalize():
    async def step(ctx: Ctx):
        # The project goes live even with skipped features — a 11-of-12 app is a real app —
        # but the run carries the honest count, and the chat thread records it so the user
        # sees what was NOT built without having to read the step list.
        await store.set_project_status(ctx.project_id, "live")
        summary = engine.run_summary(ctx.state)
        skipped = ctx.state.get("skipped") or []
        if skipped:
            await store.append_message(
                ctx.project_id, "system",
                "Some features could not be built and were skipped: " + summary,
                {"skipped": [{"feature": s.get("label"), "reason": s.get("reason")}
                             for s in skipped]})
        return f"project live — {summary}"
    return step


# --------------------------------------------------------------------------
# resume support — rebuilding ctx.state for a run that is picked up mid-flight
# --------------------------------------------------------------------------
async def rehydrate_state(project_id: str, user_id: str, email: Optional[str]) -> dict:
    """Reconstruct the pipeline's `ctx.state` from durable storage.

    Resuming skips every step already marked `done`, which also means those steps never run
    their side effect of *populating ctx.state* — so a naive resume would blow up on the first
    step that reads `state["workspace"]`. Everything the steps put in state is derivable from
    a durable source, so we re-derive it here instead:

        gitea_user/token       <- gitea.ensure_user (idempotent)
        repo                   <- projects.gitea_repo
        workspace              <- re-clone/fetch of that repo (idempotent)
        db_password/app_secret <- project_secrets (encrypted at rest)
        tech_plan              <- docs/TECHNICAL-PLAN.md in the workspace

    Best-effort by design: a run that died BEFORE create_repo has nothing to check out, and
    the (still not-done) prelude steps will simply create it.
    """
    state: dict = {"changes": []}
    proj = await store.get_project(project_id)
    acct = await gitea.ensure_user(user_id, email)
    state["gitea_user"] = acct["gitea_username"]
    state["gitea_token"] = acct["token"]
    state["repo"] = (proj or {}).get("gitea_repo") or f"app-{project_id}"
    state["brief"] = (proj or {}).get("prompt", "") or ""
    try:
        ws = await workspace.checkout(
            project_id, state["gitea_user"], state["repo"], state["gitea_token"],
            author_name=state["gitea_user"],
            author_email=email or f"{state['gitea_user']}@builderapps.osmike.com")
        state["workspace"] = str(ws)
    except Exception as e:  # noqa: BLE001 — repo may not exist yet; the prelude will make it
        logger.info("resume %s: no checkout yet (%s)", project_id, e)
    dbp = await store.get_secret(project_id, "db_password")
    aps = await store.get_secret(project_id, "app_secret")
    if dbp:
        state["db_password"] = dbp
    if aps:
        state["app_secret"] = aps
    if state.get("workspace"):
        try:
            state["tech_plan"] = workspace.read_file_capped(
                project_id, "docs/TECHNICAL-PLAN.md") or ""
        except Exception:  # noqa: BLE001
            state["tech_plan"] = ""
        try:
            state["recent_commits"] = await workspace.recent_commits(project_id, n=8)
        except Exception:  # noqa: BLE001
            state["recent_commits"] = []
    return state


def _stabilize_backlog(items: list[str], stored_total: int, n_prelude: int) -> list[str]:
    """Keep the step INDICES of a resumed run identical to the original run.

    The tail is `data_layer + N features + final_deploy + runtime_qa + finalize`, so the
    original run's feature count is recoverable from its recorded total_steps. The backlog is
    re-parsed from the same TECHNICAL-PLAN.md and is normally identical — but if that doc were
    ever regenerated, a different N would shift every later idx and corrupt the resume. So pin
    N to what the run actually recorded.
    """
    if stored_total <= 0:
        return items
    want = stored_total - n_prelude - 4        # data_layer + final_deploy + runtime_qa + finalize
    if want <= 0 or want == len(items):
        return items
    logger.warning("resume: backlog re-parsed to %d items but the run recorded %d — pinning",
                   len(items), want)
    if len(items) > want:
        return items[:want]
    return items + ["Complete the app per the technical plan: finish any route, page or "
                    "wiring that is still missing."] * (want - len(items))


# --------------------------------------------------------------------------
# create-pipeline assembly (two-pass: spine+strategy -> derive backlog -> tail)
# --------------------------------------------------------------------------
async def run_create(project_id: str, run_id: int, user_id: str, email: Optional[str],
                     emit: Callable[[dict], None], *, resume: bool = False) -> dict:
    """Run the full create pipeline. The backlog is derived from the strategy docs during the
    run; because the step list must be known to the engine up front (for stable idx), we build
    the strategy+backlog first (deterministically resumable), then assemble the full step list.

    `resume=True` re-enters an interrupted run: ctx.state is rebuilt from durable storage
    (`rehydrate_state`) and the engine skips every step already recorded `done`.
    """
    proj = await store.get_project(project_id)
    brief = (proj or {}).get("prompt", "") or ""
    resumed_state = await rehydrate_state(project_id, user_id, email) if resume else None

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
                                             base_idx=0, state=resumed_state)
            backlog_items = state.get("backlog", [])
            if not backlog_items:
                # Resuming past a `done` parse_backlog step: re-derive it from the same
                # TECHNICAL-PLAN.md (deterministic), then pin its length to the recorded run
                # so every later step keeps its original idx.
                run_row = await store.get_run(run_id)
                stored_total = int((run_row or {}).get("total_steps") or 0)
                backlog_items = backlog_mod.parse_backlog(
                    state.get("tech_plan", "") or "", cap=_MAX_FEATURES)
                backlog_items = _stabilize_backlog(backlog_items, stored_total,
                                                   len(prelude_steps))
                if not backlog_items:
                    raise RuntimeError(
                        "cannot resume: no backlog and no TECHNICAL-PLAN.md to derive one")
                state["backlog"] = backlog_items

            state["feature_total"] = len(backlog_items)

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
        res = await agentic.run_agent(
            project_id=ctx.project_id, run_id=ctx.run_id, step="plan_and_apply",
            brief=ctx.state["brief"], tech_plan=ctx.state["tech_plan"],
            task=request_text, recent_changes=ctx.state.get("recent_commits", []),
            extra="This app is ALREADY LIVE with real user data. Read every file you touch "
                  "first, and if the change spans the client and the server, read the server "
                  "endpoint's actual response shape before you edit the client — never rename "
                  "an existing endpoint's keys to make the frontend work.",
            mode="update", emit=ctx.emit)
        ctx.state["changed_files"] = list(res["changed"])
        return (f"applied minimal diff -> {res['changed'] or 'no file changes'} "
                f"({res['tool_calls']} tool calls; {res['summary'][:160]})")

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
        except Exception as e:  # noqa: BLE001
            # Try to REPAIR before reverting: the agent still has the change in the tree and
            # can tail the container's own crash (app_logs). Reverting a user's requested
            # change because of a typo it could have fixed is a worse outcome than a retry.
            logger.warning("update deploy failed for %s, one repair pass: %s", ctx.project_id, e)
            ctx.emit({"type": "progress", "stage": "deploy",
                      "detail": "deploy failed — repair pass before rollback"})
            repaired = False
            try:
                await agentic.run_agent(
                    project_id=ctx.project_id, run_id=ctx.run_id, step="plan_and_apply_fix",
                    brief=ctx.state["brief"], tech_plan=ctx.state["tech_plan"],
                    task=f"The change just applied for '{request_text[:120]}' broke the build "
                         f"or the health gate. Diagnose and fix it with the smallest possible "
                         f"change — do NOT undo the requested change.",
                    extra=f"Deploy/health error:\n{str(e)[:2000]}\n"
                          f"Files just changed: "
                          f"{', '.join(ctx.state.get('changed_files') or []) or '(none)'}\n"
                          "Use app_logs to see what the container itself reported.",
                    recent_changes=ctx.state.get("recent_commits", []), mode="fix",
                    require_change=False, emit=ctx.emit)
                res = await deployer.deploy(
                    ctx.project_id, ws, db_password=ctx.state["db_password"],
                    app_secret=ctx.state["app_secret"], title=(proj or {}).get("title", ""))
                repaired = True
            except Exception as e2:  # noqa: BLE001 — repair failed: revert to last commit
                logger.warning("update repair failed for %s, rolling back: %s",
                               ctx.project_id, e2)
                await workspace.checkout(ctx.project_id, ctx.state["gitea_user"],
                                         ctx.state["repo"], ctx.state["gitea_token"])
                if last_good:
                    norm.write_text(last_good, "utf-8")
                await deployer.deploy(
                    ctx.project_id, ws, db_password=ctx.state["db_password"],
                    app_secret=ctx.state["app_secret"], title=(proj or {}).get("title", ""))
                raise RuntimeError(f"update reverted (deploy failed): {e}")
            if repaired:
                ctx.state["repaired"] = True
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
            commit_fix=commit_fix, run_id=ctx.run_id)
        ctx.emit({"type": "qa", "final": True, "clean": report["clean"],
                  "rounds": report["rounds"], "critic": report["critic"],
                  "errors": report["errors"][:10], "network": report["network"][:10],
                  "semantic": report.get("semantic", [])[:10],
                  "flows_checked": report.get("flows_checked", 0),
                  "flows_passed": report.get("flows_passed", 0)})
        return (f"QA {'clean' if report['clean'] else 'live-with-warnings'} "
                f"(flows {report.get('flows_passed', 0)}/{report.get('flows_checked', 0)} "
                f"rendered): {report['critic'][:150]}")

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
                     request_text: str, emit: Callable[[dict], None], *,
                     resume: bool = False) -> dict:
    steps = build_update_steps(user_id, email, request_text)
    resumed_state = await rehydrate_state(project_id, user_id, email) if resume else None
    lk = workspace.lock(project_id)
    async with lk:
        try:
            return await engine.run_partial(project_id, run_id, steps, emit, base_idx=0,
                                            state=resumed_state, finish=True,
                                            total=len(steps))
        except Exception:
            await store.set_project_status(project_id, "failed")
            raise
