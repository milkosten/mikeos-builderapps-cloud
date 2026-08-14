"""Ship HEAD (phase 30) — build + health-gate the commit that is ALREADY in git, or roll back.

This is the chain behind an assistant's `request_deploy`, and it is deliberately **not** the
build pipeline.

    checkout origin/HEAD  ->  compose normalizer  ->  docker build  ->  HEALTH GATE
                                                              |
                                            green ------------+------------ red
                                              |                              |
                                    record the deployment        roll the repo back to the
                                    against the beat, live       last commit that passed the
                                                                 gate, redeploy THAT, and
                                                                 record the beat `failed`

## Why this is its own module and not another `pipeline.py` function

Mike's line, and it is the core distinction in this product: **assistants are completely
disconnected from the build pipeline.** The pipeline is strict and ordered — plan, backlog,
`build_NN`, QA — and it produces v1 of the software. An assistant has free judgment and
*evolves* the product from then on; its coding engine is Pi, inside its own container.

So `request_deploy` must NOT mean "run the update pipeline". The update pipeline re-plans a
minimal diff with its own LLM pass, which would duplicate — and argue with — the work the
assistant's Pi run just did and already pushed. `request_deploy` means exactly one thing:

    "I pushed a commit. Ship the current HEAD."

What is left after removing the reasoning is pure **infrastructure**: the compose normalizer
(the isolation guardrail), the health gate, and the rollback. That is what stops a bad
unattended commit from taking a live app down, and it is what lets the assistant container
stay on the right side of the boundary that matters — **it never gets the Docker socket.**
Docker access is host root; the assistant asks, the control plane acts.

## Never leave the app broken

Every failure path ends with a running app:
  * the health gate goes red        -> roll back to the last good commit and redeploy it
  * the rollback deploy ALSO fails  -> say so, loudly, in the run error and the beat log
  * there is no known good commit   -> refuse to guess; leave the stack alone and fail

The rollback is a FORWARD commit (`workspace.roll_back_to`), never a force-push: the bad
commit stays in the log where a human can read it.
"""
import logging
import os
from typing import Callable, Optional

from server import deployer, gitea, store, workspace
from server.harness import engine
from server.harness.engine import Ctx, Step

logger = logging.getLogger(__name__)

SITES_BASE = os.environ.get("SITES_BASE", "builderapps.osmike.com")

# A ship run is three steps and they are all load-bearing (nothing here is skippable —
# `engine.is_skippable` only ever says yes to a `build_NN` backlog feature).
STEP_NAMES = ("ship_checkout", "ship_deploy", "ship_record")


def _guard_placeholder(project_id: str) -> str:
    """Make sure we are not about to put the builder's holding page back on the air.

    The template ships `public/index.html` — "Your app is being built" — and `express.static`
    is mounted before the app's own routes, so that file SHADOWS a server-rendered `/`. The
    create pipeline deletes it at `final_deploy`, but it records its deployment sha *before*
    the commit that carries the deletion. So the very first "last good commit" for a project
    is one whose tree still contains the placeholder.

    Which means a rollback — the code whose entire job is "never leave the app broken" —
    could faithfully restore a healthy stack serving the holding page, pass its own health
    gate (which only reads /health) and report success. Exactly the failure that once shipped
    a finished app with the builder's placeholder as its public home page.

    So: before every build here, forward or rollback, drop the placeholder if the app has a
    `/` of its own. Cheap, idempotent, and it closes the hole at the point of use.
    """
    try:
        if workspace.placeholder_state(project_id) == "shadowing":
            workspace.drop_placeholder(project_id)
            return "removed the build placeholder that was shadowing the app's own /"
    except Exception as e:  # noqa: BLE001 — never block a deploy on this
        logger.info("placeholder guard skipped for %s: %s", project_id, e)
    return ""


async def _secrets_for(project_id: str) -> tuple[str, str]:
    """The project's existing db password + app secret. A ship NEVER mints new ones: this
    deploys an app that already has data, and a fresh DB_PASSWORD would lock it out of its
    own Postgres."""
    dbp = await store.get_secret(project_id, "db_password")
    aps = await store.get_secret(project_id, "app_secret")
    if not dbp or not aps:
        raise RuntimeError(
            "this project has no stored db_password/app_secret — it has never been built, "
            "so there is nothing to ship")
    return dbp, aps


def build_ship_steps(user_id: str, email: Optional[str], *, reason: str,
                     assistant_id: Optional[int] = None,
                     beat_id: Optional[int] = None,
                     outcome: Optional[dict] = None) -> list[Step]:
    """The three steps. `outcome` is a caller-owned dict the steps fill in, so the caller can
    still learn what happened on the failure path (where no state is returned)."""
    out = outcome if outcome is not None else {}

    async def s_checkout(ctx: Ctx):
        acct = await gitea.ensure_user(user_id, email)
        ctx.state["gitea_user"] = acct["gitea_username"]
        ctx.state["gitea_token"] = acct["token"]
        proj = await store.get_project(ctx.project_id)
        ctx.state["repo"] = (proj or {}).get("gitea_repo") or f"app-{ctx.project_id}"
        # Fetch + hard reset to origin/HEAD: whatever the assistant actually pushed is what
        # gets built. We never trust a sha the container claimed — git is the record.
        await workspace.checkout(ctx.project_id, ctx.state["gitea_user"],
                                 ctx.state["repo"], ctx.state["gitea_token"])
        head = await workspace.head_sha(ctx.project_id)
        good = await store.last_good_sha(ctx.project_id)
        ctx.state["head_sha"] = head
        ctx.state["last_good_sha"] = good or ""
        incoming = await workspace.commits_between(ctx.project_id, good) if good else []
        ctx.state["incoming_commits"] = incoming
        out["head_sha"] = head
        out["last_good_sha"] = good or ""
        return (f"HEAD {head[:8]}; last good {good[:8] if good else '(none recorded)'}; "
                f"{len(incoming)} new commit(s): {'; '.join(incoming[:5]) or 'none'}")

    async def s_deploy(ctx: Ctx):
        db_password, app_secret = await _secrets_for(ctx.project_id)
        proj = await store.get_project(ctx.project_id)
        title = (proj or {}).get("title", "")
        ws = workspace.path_for(ctx.project_id)
        await store.set_project_status(ctx.project_id, "deploying")
        note = _guard_placeholder(ctx.project_id)
        if note:
            ctx.emit({"type": "progress", "stage": "deploy", "detail": note})
        ctx.emit({"type": "progress", "stage": "deploy",
                  "detail": f"building {ctx.state.get('head_sha', '')[:8]} and "
                            f"health-gating it"})
        try:
            res = await deployer.deploy(
                ctx.project_id, ws, db_password=db_password, app_secret=app_secret,
                title=title, assistant_id=assistant_id, beat_id=beat_id)
        except Exception as first:  # noqa: BLE001
            # ---- THE ROLLBACK -------------------------------------------------------
            # No repair pass, no second LLM opinion: this module has no reasoning in it by
            # design. The commit failed the gate, so the commit goes away and the last
            # thing that WORKED goes back on the air.
            reason_txt = str(first)[:600]
            logger.warning("ship: %s failed the health gate at %s — rolling back: %s",
                           ctx.project_id, ctx.state.get("head_sha", "")[:8], reason_txt)
            ctx.emit({"type": "progress", "stage": "deploy",
                      "detail": "health gate failed — rolling back to the last good commit"})
            out["failed_sha"] = ctx.state.get("head_sha", "")
            out["failure"] = reason_txt
            good = ctx.state.get("last_good_sha") or ""
            if not good:
                out["rollback"] = "impossible"
                raise RuntimeError(
                    f"deploy of {ctx.state.get('head_sha', '')[:8]} failed the health gate "
                    f"and there is NO recorded good commit to roll back to — the stack was "
                    f"left as it is. Original error: {reason_txt}")
            new_sha = await workspace.roll_back_to(
                ctx.project_id, good,
                f"revert: roll back to {good[:8]} — "
                f"{ctx.state.get('head_sha', '')[:8]} failed the health gate\n\n"
                f"Automatic rollback by the control plane after an assistant deploy did not "
                f"go green. The reverted commit is still in the log above.\n\n"
                f"Reason: {reason_txt[:400]}",
                ctx.state["gitea_user"], ctx.state["repo"], ctx.state["gitea_token"])
            out["rollback_sha"] = new_sha or ""
            try:
                await deployer.deploy(
                    ctx.project_id, ws, db_password=db_password, app_secret=app_secret,
                    title=title, assistant_id=assistant_id, beat_id=beat_id)
            except Exception as second:  # noqa: BLE001
                out["rollback"] = "failed"
                await store.set_project_status(ctx.project_id, "failed")
                raise RuntimeError(
                    f"deploy failed AND the rollback to {good[:8]} also failed — the app may "
                    f"be down. Original: {reason_txt} | Rollback: {str(second)[:400]}")
            out["rollback"] = "healthy"
            # The app is up again on the last good commit, so the PROJECT is live even
            # though the RUN failed. Those are two different facts and the code says so.
            await store.set_project_status(ctx.project_id, "live")
            raise RuntimeError(
                f"health gate failed for {ctx.state.get('head_sha', '')[:8]}; rolled back to "
                f"{good[:8]} and the app is healthy again. Reason: {reason_txt}")
        ctx.state["public_health"] = res["public_health"]
        out["deployed_sha"] = res.get("git_sha") or ctx.state.get("head_sha", "")
        out["deployment_id"] = res.get("deployment_id")
        out["health"] = res["public_health"]
        ctx.emit({"type": "deploy",
                  "url": f"https://{ctx.project_id}.{SITES_BASE}/",
                  "health": res["public_health"]})
        return f"live on {out['deployed_sha'][:8]}: {res['public_health']}"

    async def s_record(ctx: Ctx):
        await store.set_project_status(ctx.project_id, "live")
        who = f"assistant {assistant_id}" if assistant_id else "the control plane"
        commits = ctx.state.get("incoming_commits") or []
        note = (f"Shipped {ctx.state.get('head_sha', '')[:8]} ({who}, beat {beat_id}): "
                f"{reason[:200]}"
                + (f"\n\nCommits: " + "; ".join(commits[:5]) if commits else ""))
        await store.append_message(ctx.project_id, "system", note,
                                   {"kind": "assistant_deploy",
                                    "assistant_id": assistant_id, "beat_id": beat_id,
                                    "sha": ctx.state.get("head_sha", "")})
        out["ok"] = True
        return note[:400]

    return [("ship_checkout", s_checkout), ("ship_deploy", s_deploy),
            ("ship_record", s_record)]


async def run_ship(project_id: str, run_id: int, user_id: str, email: Optional[str],
                   reason: str, emit: Callable[[dict], None], *,
                   assistant_id: Optional[int] = None, beat_id: Optional[int] = None,
                   outcome: Optional[dict] = None, resume: bool = False) -> dict:
    """Ship the project's current HEAD. Serialized on the project's workspace lock, which is
    the SAME lock the build pipeline takes — a ship can never race a build over one checkout,
    and two ships queue rather than stampede."""
    steps = build_ship_steps(user_id, email, reason=reason, assistant_id=assistant_id,
                             beat_id=beat_id, outcome=outcome)
    async with workspace.lock(project_id):
        try:
            return await engine.run_partial(project_id, run_id, steps, emit, base_idx=0,
                                            finish=True, total=len(steps))
        except Exception:
            # `s_deploy` already set the honest project status (live after a good rollback,
            # failed when even that did not come up). Only a failure BEFORE it — a checkout
            # that never happened — leaves the project mid-flight, so fix just that case.
            proj = await store.get_project(project_id)
            if proj and proj.get("status") in ("creating", "building", "deploying"):
                await store.set_project_status(project_id, "failed")
            raise
