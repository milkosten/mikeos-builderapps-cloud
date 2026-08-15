"""Data-access layer over the control-plane Postgres (schema `builderapps`).

Parameterized SQL only. Write helpers verify the affected row (never trust an
implicit success). Secret/token columns go through server.crypto before storage.
"""
import json
import logging
import secrets
import string
from typing import Any, Optional

from server import crypto
from server.db import pool

logger = logging.getLogger(__name__)

# Caddy wildcard vhost enforces exactly ^[a-z0-9]{6}$ — the shortid MUST match.
_ALPHABET = string.ascii_lowercase + string.digits


def gen_shortid() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(6))


async def alloc_shortid() -> str:
    """Return a 6-char shortid not already used by a project (collision-checked)."""
    for _ in range(20):
        sid = gen_shortid()
        exists = await pool().fetchval(
            "SELECT 1 FROM builderapps.projects WHERE id = $1", sid
        )
        if not exists:
            return sid
    raise RuntimeError("could not allocate a free shortid after 20 tries")


# ---- gitea_accounts -------------------------------------------------------
async def get_gitea_account(user_id: str) -> Optional[dict]:
    row = await pool().fetchrow(
        "SELECT user_id, gitea_username, token_enc, created_at "
        "FROM builderapps.gitea_accounts WHERE user_id = $1",
        user_id,
    )
    if not row:
        return None
    d = dict(row)
    d["token"] = crypto.decrypt(d.pop("token_enc"))
    return d


async def upsert_gitea_account(user_id: str, gitea_username: str, token: str) -> None:
    token_enc = crypto.encrypt(token)
    res = await pool().execute(
        "INSERT INTO builderapps.gitea_accounts(user_id, gitea_username, token_enc) "
        "VALUES ($1,$2,$3) "
        "ON CONFLICT (user_id) DO UPDATE SET gitea_username = EXCLUDED.gitea_username, "
        "token_enc = EXCLUDED.token_enc",
        user_id, gitea_username, token_enc,
    )
    # never-trust: an INSERT/UPDATE must report one affected row
    if not res.endswith(("INSERT 0 1", "UPDATE 1")):
        raise RuntimeError(f"gitea_accounts upsert did not affect a row: {res!r}")


# ---- projects -------------------------------------------------------------
async def create_project(*, id: str, user_id: str, gitea_owner: str, gitea_repo: str,
                         subdomain: str, title: str, prompt: str,
                         status: str = "creating", pipeline: str = "create") -> dict:
    row = await pool().fetchrow(
        "INSERT INTO builderapps.projects "
        "(id,user_id,gitea_owner,gitea_repo,subdomain,title,prompt,status,pipeline) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *",
        id, user_id, gitea_owner, gitea_repo, subdomain, title, prompt, status, pipeline,
    )
    if not row:
        raise RuntimeError("create_project returned no row")
    return dict(row)


async def set_project_title(project_id: str, title: str) -> None:
    """Rename a project. The title is cosmetic, so callers treat a failure as non-fatal."""
    await pool().execute(
        "UPDATE builderapps.projects SET title=$2, updated_at=now() WHERE id=$1",
        project_id, title,
    )


async def set_project_adopted(project_id: str, adopted: dict) -> None:
    """Phase 35 — record that this app is DERIVED WORK, in the platform's own database.

    Deliberately not only a NOTICE file in the repo: a file can be deleted by the next agent
    that tidies the tree, and then nothing in the product says where the code came from. This
    column is what makes "where did this come from?" answerable months later, and it is
    verified like every other write here rather than trusted.
    """
    res = await pool().execute(
        "UPDATE builderapps.projects SET adopted=$2::jsonb, updated_at=now() WHERE id=$1",
        project_id, json.dumps(adopted or {}),
    )
    if res != "UPDATE 1":
        raise RuntimeError(f"set_project_adopted affected {res!r} for {project_id}")


async def get_project_adopted(project_id: str) -> dict:
    raw = await pool().fetchval(
        "SELECT adopted FROM builderapps.projects WHERE id=$1", project_id)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    return raw or {}


async def set_project_status(project_id: str, status: str) -> None:
    res = await pool().execute(
        "UPDATE builderapps.projects SET status=$2, updated_at=now() WHERE id=$1",
        project_id, status,
    )
    if res != "UPDATE 1":
        raise RuntimeError(f"set_project_status affected {res!r} for {project_id}")


async def get_project(project_id: str, user_id: Optional[str] = None) -> Optional[dict]:
    if user_id is not None:
        row = await pool().fetchrow(
            "SELECT * FROM builderapps.projects WHERE id=$1 AND user_id=$2",
            project_id, user_id,
        )
    else:
        row = await pool().fetchrow(
            "SELECT * FROM builderapps.projects WHERE id=$1", project_id
        )
    if not row:
        return None
    d = dict(row)
    # asyncpg hands jsonb back as a STRING. Decode here, once, so no caller — and in
    # particular no browser — receives `"adopted": "{\"repo\": ...}"` and has to guess that
    # the value it was given is itself JSON.
    if isinstance(d.get("adopted"), str):
        try:
            d["adopted"] = json.loads(d["adopted"])
        except Exception:  # noqa: BLE001 — a corrupt cell must not 500 the project page
            d["adopted"] = None
    return d


async def list_projects(user_id: str) -> list[dict]:
    rows = await pool().fetch(
        "SELECT id,title,status,subdomain,pipeline,created_at,updated_at "
        "FROM builderapps.projects WHERE user_id=$1 ORDER BY created_at DESC LIMIT 200",
        user_id,
    )
    return [dict(r) for r in rows]


# ---- project_secrets ------------------------------------------------------
async def project_subnet(project_id: str) -> str:
    """The project's own private /24, allocated once and pinned (phase 28).

    Docker's default address pools top out at ~31 networks; the 21st app on 242 could not be
    created at all. Handing compose an EXPLICIT ipam subnet from the (entirely unused)
    10.0.0.0/8 space sidesteps the pool completely — no dockerd restart, no bouncing 110
    production containers. 10.100.0.0 .. 10.255.255.0 as /24s = ~40 000 projects.
    """
    idx = await pool().fetchval(
        "UPDATE builderapps.projects "
        "SET net_index = COALESCE(net_index, nextval('builderapps.project_net_seq')::int) "
        "WHERE id=$1 RETURNING net_index", project_id)
    if idx is None:
        raise RuntimeError(f"project_subnet: no project row for {project_id}")
    n = int(idx) - 1
    if n < 0 or n >= 156 * 256:
        raise RuntimeError(f"project_subnet: allocation {idx} is outside 10.100-255.x")
    return f"10.{100 + (n // 256)}.{n % 256}.0/24"


async def put_secret(project_id: str, key: str, value: str) -> None:
    value_enc = crypto.encrypt(value)
    res = await pool().execute(
        "INSERT INTO builderapps.project_secrets(project_id,secret_key,value_enc) "
        "VALUES ($1,$2,$3) "
        "ON CONFLICT (project_id,secret_key) DO UPDATE SET value_enc=EXCLUDED.value_enc",
        project_id, key, value_enc,
    )
    if not res.endswith(("INSERT 0 1", "UPDATE 1")):
        raise RuntimeError(f"put_secret did not affect a row: {res!r}")


async def get_secret(project_id: str, key: str) -> Optional[str]:
    enc = await pool().fetchval(
        "SELECT value_enc FROM builderapps.project_secrets "
        "WHERE project_id=$1 AND secret_key=$2",
        project_id, key,
    )
    return crypto.decrypt(enc) if enc else None


async def list_secrets(project_id: str) -> list[dict]:
    """[{key, value}] with the PLAINTEXT value — callers MUST mask before returning it to a
    client, and must never log it. Bounded to 200 rows."""
    rows = await pool().fetch(
        "SELECT secret_key FROM builderapps.project_secrets WHERE project_id=$1 "
        "ORDER BY secret_key LIMIT 200", project_id,
    )
    out = []
    for r in rows:
        key = r["secret_key"]
        try:
            value = await get_secret(project_id, key) or ""
        except Exception as e:  # noqa: BLE001 — never leak the value into the log
            logger.warning("could not decrypt secret %s for %s: %s", key, project_id, type(e).__name__)
            value = ""
        out.append({"key": key, "value": value})
    return out


# ---- deployments ----------------------------------------------------------
async def create_deployment(project_id: str, image_tag: str = "",
                            compose_hash: str = "", git_sha: str = "",
                            assistant_id: Optional[int] = None,
                            beat_id: Optional[int] = None) -> int:
    """Open a deployment row.

    `git_sha` is what makes a ROLLBACK possible at all: the newest row that reached
    `healthy` is by definition the last commit that passed the health gate — the thing a
    failed assistant deploy has to go back to. `assistant_id`/`beat_id` make an unattended
    deploy attributable to the exact beat that caused it.
    """
    dep_id = await pool().fetchval(
        "INSERT INTO builderapps.deployments"
        "(project_id,image_tag,compose_hash,status,git_sha,assistant_id,beat_id) "
        "VALUES ($1,$2,$3,'deploying',$4,$5,$6) RETURNING id",
        project_id, image_tag, compose_hash, (git_sha or "")[:64], assistant_id, beat_id,
    )
    if dep_id is None:
        raise RuntimeError("create_deployment returned no id")
    return int(dep_id)


async def list_deployments(project_id: str, limit: int = 50) -> list[dict]:
    rows = await pool().fetch(
        "SELECT id,image_tag,status,health,git_sha,assistant_id,beat_id,"
        "       started_at,finished_at "
        "FROM builderapps.deployments WHERE project_id=$1 "
        "ORDER BY started_at DESC LIMIT $2",
        project_id, max(1, min(int(limit), 200)),
    )
    return [dict(r) for r in rows]


async def last_good_sha(project_id: str) -> Optional[str]:
    """The most recent commit that actually passed the health gate — the rollback target.

    Derived from the deployments table rather than kept in a `projects.last_good_sha`
    column on purpose: one writer (`finish_deployment`), so there is no second source of
    truth to drift out of step with reality. Returns None for a project that has never had
    a green deploy recorded WITH a sha — and the caller must then refuse to "roll back",
    because it does not know where to.
    """
    return await pool().fetchval(
        "SELECT git_sha FROM builderapps.deployments "
        "WHERE project_id=$1 AND status='healthy' AND git_sha <> '' "
        "ORDER BY started_at DESC LIMIT 1", project_id)


async def finish_deployment(dep_id: int, status: str, health: str = "") -> None:
    res = await pool().execute(
        "UPDATE builderapps.deployments SET status=$2, health=$3, finished_at=now() "
        "WHERE id=$1",
        dep_id, status, health[:4000],
    )
    if res != "UPDATE 1":
        raise RuntimeError(f"finish_deployment affected {res!r} for {dep_id}")


# ---- repair episodes (phase 31) -------------------------------------------
async def open_repair(project_id: str) -> Optional[dict]:
    """The repair episode currently running for this project, if any."""
    row = await pool().fetchrow(
        "SELECT * FROM builderapps.deploy_repairs "
        "WHERE project_id=$1 AND status='open'", project_id)
    return dict(row) if row else None


async def record_repair_failure(project_id: str, *, assistant_id: Optional[int],
                                failed_sha: str, stage: str, signature: str) -> dict:
    """Fold one red deploy into this project's repair episode and return the decision inputs.

    Returns {episode_id, attempts, previous_signature, repeat, first, origin_sha}. It decides
    nothing about what to do next — the caller applies the bounds — but it is the single place
    the counting happens, so the budget cannot be double-read.
    """
    prev = await open_repair(project_id)
    if not prev:
        row = await pool().fetchrow(
            "INSERT INTO builderapps.deploy_repairs"
            "(project_id,assistant_id,origin_sha,last_sha,stage,signature,attempts,status) "
            "VALUES ($1,$2,$3,$3,$4,$5,0,'open') RETURNING *",
            project_id, assistant_id, (failed_sha or "")[:64], stage[:32], signature[:400])
        return {"episode_id": int(row["id"]), "attempts": 0, "previous_signature": "",
                "repeat": False, "first": True, "origin_sha": str(row["origin_sha"] or "")}
    row = await pool().fetchrow(
        "UPDATE builderapps.deploy_repairs SET last_sha=$2, stage=$3, signature=$4, "
        "updated_at=now() WHERE id=$1 RETURNING *",
        int(prev["id"]), (failed_sha or "")[:64], stage[:32], signature[:400])
    return {"episode_id": int(row["id"]), "attempts": int(prev["attempts"]),
            "previous_signature": str(prev["signature"] or ""),
            "repeat": str(prev["signature"] or "") == signature,
            "first": False, "origin_sha": str(prev["origin_sha"] or "")}


async def count_repair_attempt(episode_id: int) -> int:
    """A repair beat is about to be dispatched — charge it to the episode's budget."""
    n = await pool().fetchval(
        "UPDATE builderapps.deploy_repairs SET attempts=attempts+1, updated_at=now() "
        "WHERE id=$1 RETURNING attempts", episode_id)
    if n is None:
        raise RuntimeError(f"count_repair_attempt: no episode {episode_id}")
    return int(n)


async def close_repair(project_id: str, status: str = "resolved", detail: str = "") -> bool:
    """Close the open episode. Called on a GREEN deploy (resolved) and on escalation."""
    res = await pool().execute(
        "UPDATE builderapps.deploy_repairs SET status=$2, detail=$3, updated_at=now() "
        "WHERE project_id=$1 AND status='open'",
        project_id, status[:16], (detail or "")[:2000])
    return res.endswith("UPDATE 1")


async def recent_repairs(project_id: str, limit: int = 5) -> list[dict]:
    rows = await pool().fetch(
        "SELECT id,origin_sha,last_sha,stage,attempts,status,detail,created_at,updated_at "
        "FROM builderapps.deploy_repairs WHERE project_id=$1 "
        "ORDER BY created_at DESC LIMIT $2", project_id, max(1, min(int(limit), 50)))
    return [dict(r) for r in rows]


# ---- pipeline runs / steps ------------------------------------------------
async def create_run(project_id: str, kind: str, request: str, total_steps: int,
                     assistant_id: Optional[int] = None,
                     beat_id: Optional[int] = None) -> int:
    """Open a pipeline run. `kind` ∈ {create, update, deploy}.

    `assistant_id`/`beat_id` are set when an ASSISTANT caused this run, so every unattended
    build is traceable back to the beat that triggered it — and the beat record links
    forward to the run. An agent that can ship code must leave a trail in both directions.
    """
    run_id = await pool().fetchval(
        "INSERT INTO builderapps.pipeline_runs"
        "(project_id,kind,request,total_steps,assistant_id,beat_id) "
        "VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
        project_id, kind, request, total_steps, assistant_id, beat_id,
    )
    if run_id is None:
        raise RuntimeError("create_run returned no id")
    return int(run_id)


async def set_run_total(run_id: int, total_steps: int) -> None:
    await pool().execute(
        "UPDATE builderapps.pipeline_runs SET total_steps=$2 WHERE id=$1",
        run_id, total_steps,
    )


async def finish_run(run_id: int, status: str, error: str = "", summary: str = "") -> None:
    """Close a run. `summary` is the HONEST outcome line (phase 28): a run that finished with
    skipped features is `done` but must never read as complete success, so the caller passes
    e.g. "11 of 12 features built; 1 skipped: ...". Stored and echoed in the terminal SSE."""
    await pool().execute(
        "UPDATE builderapps.pipeline_runs SET status=$2, finished_at=now(), error=$3, "
        "summary=$4 WHERE id=$1",
        run_id, status, (error or "")[:4000], (summary or "")[:4000],
    )


async def upsert_step(run_id: int, idx: int, name: str, status: str, log: str = "") -> None:
    res = await pool().execute(
        "INSERT INTO builderapps.pipeline_steps(run_id,idx,name,status,log,ts) "
        "VALUES ($1,$2,$3,$4,$5,now()) "
        "ON CONFLICT (run_id,idx) DO UPDATE SET status=EXCLUDED.status, "
        "name=EXCLUDED.name, log=EXCLUDED.log, ts=now()",
        run_id, idx, name, status, log[:8000],
    )
    if not res.endswith(("INSERT 0 1", "UPDATE 1")):
        raise RuntimeError(f"upsert_step did not affect a row: {res!r}")
    if status in ("running", "done", "failed"):
        # A step transition is also proof of life — keep the heartbeat fresh so the janitor
        # never reaps a run that is demonstrably making progress.
        await pool().execute(
            "UPDATE builderapps.pipeline_runs SET current_step=$2, heartbeat_at=now() "
            "WHERE id=$1",
            run_id, idx,
        )


# ---- run liveness: heartbeat / claim / sweep (phase 19) -------------------
# A run only counts as ALIVE while some api process keeps bumping `heartbeat_at`. Everything
# else (boot sweep, janitor) is derived from that one fact, so a redeploy / OOM-kill / client
# disconnect can never leave a run in limbo.
async def get_run(run_id: int) -> Optional[dict]:
    row = await pool().fetchrow(
        "SELECT * FROM builderapps.pipeline_runs WHERE id=$1", run_id)
    return dict(row) if row else None


async def heartbeat_run(run_id: int, owner: str) -> bool:
    """Bump heartbeat_at. Returns False when this process no longer owns the run (another
    process claimed it), which tells the caller to stand down instead of double-running."""
    res = await pool().execute(
        "UPDATE builderapps.pipeline_runs SET heartbeat_at=now() "
        "WHERE id=$1 AND owner=$2 AND status='running'",
        run_id, owner,
    )
    return res == "UPDATE 1"


async def claim_run(run_id: int, owner: str, stale_sec: int,
                    any_other_owner: bool = False) -> Optional[dict]:
    """Atomically take ownership of a run. This single UPDATE is the cross-process mutex:
    two processes sweeping the same orphan race on it and exactly one wins.

    Normally a run is only claimable when it is unowned or its heartbeat has gone stale.
    `any_other_owner=True` (the BOOT sweep) also claims a run whose heartbeat is still fresh
    but which belongs to a *different* instance: at startup that owner is by definition a
    process that no longer exists — the previous container. Without this, recovery after a
    redeploy had to wait out the whole staleness window before anything happened.

    Returns the claimed row, or None if the claim did not apply.
    """
    if any_other_owner:
        row = await pool().fetchrow(
            "UPDATE builderapps.pipeline_runs SET owner=$2, attempts=attempts+1, "
            "heartbeat_at=now() "
            "WHERE id=$1 AND status='running' AND coalesce(owner,'') <> $2 RETURNING *",
            run_id, owner,
        )
    else:
        row = await pool().fetchrow(
            "UPDATE builderapps.pipeline_runs SET owner=$2, attempts=attempts+1, "
            "heartbeat_at=now() "
            "WHERE id=$1 AND status='running' "
            "  AND (owner = '' OR owner IS NULL OR heartbeat_at IS NULL "
            "       OR heartbeat_at < now() - make_interval(secs => $3::double precision)) "
            "RETURNING *",
            run_id, owner, float(stale_sec),
        )
    return dict(row) if row else None


async def claim_new_run(run_id: int, owner: str) -> bool:
    """Take ownership of a run this process just created (no staleness test needed)."""
    res = await pool().execute(
        "UPDATE builderapps.pipeline_runs SET owner=$2, attempts=attempts+1, "
        "heartbeat_at=now() WHERE id=$1 AND status='running'",
        run_id, owner,
    )
    return res == "UPDATE 1"


async def release_run(run_id: int, owner: str) -> None:
    """Drop ownership without changing status (used when a run is cancelled at shutdown, so
    the next boot's sweep sees an unowned running run and resumes it immediately)."""
    await pool().execute(
        "UPDATE builderapps.pipeline_runs SET owner='' WHERE id=$1 AND owner=$2",
        run_id, owner,
    )


async def stale_running_runs(stale_sec: int, limit: int = 50) -> list[dict]:
    """Runs still marked `running` whose heartbeat has gone stale — i.e. orphans."""
    rows = await pool().fetch(
        "SELECT * FROM builderapps.pipeline_runs "
        "WHERE status='running' "
        "  AND (heartbeat_at IS NULL "
        "       OR heartbeat_at < now() - make_interval(secs => $1::double precision)) "
        "ORDER BY created_at LIMIT $2",
        float(stale_sec), max(1, min(int(limit), 200)),
    )
    return [dict(r) for r in rows]


async def running_runs(limit: int = 100) -> list[dict]:
    """Every run still marked `running`, oldest first."""
    rows = await pool().fetch(
        "SELECT * FROM builderapps.pipeline_runs WHERE status='running' "
        "ORDER BY created_at LIMIT $1", max(1, min(int(limit), 500)),
    )
    return [dict(r) for r in rows]


async def fail_stuck_steps(run_id: int, reason: str) -> int:
    """Mark any step left `running` on a dead run as failed, so the UI stops spinning on it.
    Returns the number of rows changed."""
    res = await pool().execute(
        "UPDATE builderapps.pipeline_steps SET status='failed', "
        "log = left(coalesce(log,'') || $2, 8000), ts=now() "
        "WHERE run_id=$1 AND status='running'",
        run_id, f"\n[interrupted] {reason}"[:2000],
    )
    try:
        return int(str(res).rsplit(" ", 1)[-1])
    except Exception:  # noqa: BLE001
        return 0


async def get_run_with_steps(run_id: int) -> Optional[dict]:
    run = await pool().fetchrow(
        "SELECT * FROM builderapps.pipeline_runs WHERE id=$1", run_id
    )
    if not run:
        return None
    steps = await pool().fetch(
        "SELECT idx,name,status,log,ts FROM builderapps.pipeline_steps "
        "WHERE run_id=$1 ORDER BY idx", run_id,
    )
    d = dict(run)
    d["steps"] = [dict(s) for s in steps]
    return d


# ---- messages (conversation / QA thread) ----------------------------------
async def append_message(project_id: str, role: str, text: str,
                         meta: Optional[dict] = None) -> None:
    """Append one entry to the project's messages thread (jsonb array). Idempotent-safe:
    upserts the row and appends. Timestamps are ISO-8601."""
    import time as _t
    entry = {"role": role, "text": (text or "")[:4000],
             "ts": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())}
    if meta:
        entry["meta"] = meta
    res = await pool().execute(
        "INSERT INTO builderapps.messages(project_id, thread, updated_at) "
        "VALUES ($1, jsonb_build_array($2::jsonb), now()) "
        "ON CONFLICT (project_id) DO UPDATE SET "
        "thread = builderapps.messages.thread || $2::jsonb, updated_at = now()",
        project_id, json.dumps(entry),
    )
    if not res.endswith(("INSERT 0 1", "UPDATE 1")):
        raise RuntimeError(f"append_message did not affect a row: {res!r}")


async def latest_run_for(project_id: str) -> Optional[dict]:
    run = await pool().fetchrow(
        "SELECT id FROM builderapps.pipeline_runs WHERE project_id=$1 "
        "ORDER BY created_at DESC LIMIT 1", project_id,
    )
    return await get_run_with_steps(int(run["id"])) if run else None


# Durable chat history: the UI must rebuild a reloaded /builder page from Postgres, never
# from browser storage (so it survives a reload AND follows the user across devices).
_MAX_THREAD = 100
_MAX_MSG_CHARS = 4000


def sanitize_messages(items: Any) -> list[dict]:
    """Keep only {role,text}: role coerced to user/assistant, empty text dropped, each text
    clamped, and the thread capped to the last _MAX_THREAD entries (bounded memory)."""
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = it.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        role = "user" if str(it.get("role", "")).lower() in ("user", "update") else "assistant"
        out.append({"role": role, "text": text[:_MAX_MSG_CHARS]})
    return out[-_MAX_THREAD:]


async def get_messages(project_id: str) -> list[dict]:
    """Return the project's chat thread as [{role,text,ts}] (empty list when there is none).

    Pipeline-written entries carry roles like `qa`/`update`; they are normalized to the
    user/assistant vocabulary the UI renders, with `ts` preserved when present."""
    thread = await pool().fetchval(
        "SELECT thread FROM builderapps.messages WHERE project_id=$1", project_id
    )
    if not thread:
        return []
    if isinstance(thread, str):
        try:
            thread = json.loads(thread)
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(thread, list):
        return []
    out: list[dict] = []
    for it in thread[-_MAX_THREAD:]:
        if not isinstance(it, dict):
            continue
        text = it.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        role = "user" if str(it.get("role", "")).lower() in ("user", "update") else "assistant"
        entry = {"role": role, "text": text[:_MAX_MSG_CHARS]}
        if it.get("ts"):
            entry["ts"] = str(it["ts"])
        out.append(entry)
    return out


async def put_messages(project_id: str, messages: list[dict]) -> int:
    """Replace the project's chat thread with the sanitized `messages`. Returns the count
    actually stored (never-trust-200: the write is verified to have affected a row).

    Guard: an EMPTY thread never overwrites a non-empty one. This endpoint has replace
    semantics, so a client that PUTs its (still empty) local state on mount — before its
    restore GET has come back — would otherwise destroy exactly the history this table
    exists to protect. Losing the user's history is far worse than ignoring one no-op write,
    so an empty PUT against an existing thread is a no-op.
    """
    clean = sanitize_messages(messages)
    if not clean:
        existing = await pool().fetchval(
            "SELECT jsonb_array_length(thread) FROM builderapps.messages WHERE project_id=$1",
            project_id,
        )
        if existing:
            logger.warning(
                "ignoring empty messages PUT for %s — it would have wiped %d stored entries",
                project_id, existing)
            return int(existing)
    res = await pool().execute(
        "INSERT INTO builderapps.messages(project_id, thread, updated_at) "
        "VALUES ($1, $2::jsonb, now()) "
        "ON CONFLICT (project_id) DO UPDATE SET thread = EXCLUDED.thread, "
        "updated_at = now()",
        project_id, json.dumps(clean),
    )
    if not res.endswith(("INSERT 0 1", "UPDATE 1")):
        raise RuntimeError(f"put_messages did not affect a row: {res!r}")
    return len(clean)


async def all_steps_for_project(project_id: str) -> list[dict]:
    """Every step of every run for the project, oldest run first (the durable record of what
    the pipeline has actually executed). Bounded."""
    rows = await pool().fetch(
        "SELECT s.run_id, s.idx, s.name, s.status, s.log, s.ts "
        "FROM builderapps.pipeline_steps s "
        "JOIN builderapps.pipeline_runs r ON r.id = s.run_id "
        "WHERE r.project_id = $1 ORDER BY r.created_at, s.idx LIMIT 1000",
        project_id,
    )
    return [dict(r) for r in rows]


async def get_raw_thread(project_id: str) -> list[dict]:
    """The stored thread entries verbatim (role/text/ts/meta) — used by /qa, which needs the
    structured `meta` the sanitized message view drops."""
    thread = await pool().fetchval(
        "SELECT thread FROM builderapps.messages WHERE project_id=$1", project_id
    )
    if isinstance(thread, str):
        try:
            thread = json.loads(thread)
        except Exception:  # noqa: BLE001
            return []
    return [t for t in thread if isinstance(t, dict)] if isinstance(thread, list) else []


async def steps_for_latest_run(project_id: str) -> dict:
    """{"run_id","status","error","total_steps","heartbeat_at","steps":[...]} for the
    project's most recent run (steps ordered by idx). Empty list when the project has no run
    yet — never an error.

    `status`/`error` are what let a polling client distinguish "still building" from "this run
    died"; without them the UI could only ever show a spinner.
    """
    run = await pool().fetchrow(
        "SELECT id,status,error,summary,total_steps,heartbeat_at,finished_at "
        "FROM builderapps.pipeline_runs WHERE project_id=$1 "
        "ORDER BY created_at DESC LIMIT 1", project_id,
    )
    if not run:
        return {"run_id": None, "status": None, "steps": []}
    rows = await pool().fetch(
        "SELECT idx,name,status,log,ts FROM builderapps.pipeline_steps "
        "WHERE run_id=$1 ORDER BY idx", int(run["id"]),
    )
    return {"run_id": int(run["id"]), "status": run["status"],
            "error": run["error"] or "", "summary": run["summary"] or "",
            "total_steps": run["total_steps"],
            "heartbeat_at": run["heartbeat_at"], "finished_at": run["finished_at"],
            "steps": [dict(r) for r in rows]}


# ---- token / cost accounting (the Usage tab) -------------------------------
async def record_usage(*, project_id: str, run_id, step, model: str,
                       prompt_tokens: int, completion_tokens: int, cached_tokens: int,
                       cost_usd: float, cost_estimated: bool) -> None:
    """One row per LLM call. Best-effort: accounting must never break a build."""
    await pool().execute(
        "INSERT INTO builderapps.llm_usage "
        "(project_id,run_id,step,model,prompt_tokens,completion_tokens,cached_tokens,"
        " cost_usd,cost_estimated) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
        project_id, run_id, step, model, prompt_tokens, completion_tokens,
        cached_tokens, cost_usd, cost_estimated,
    )


async def usage_for_project(project_id: str) -> dict:
    """Totals + a per-step breakdown for one project."""
    tot = await pool().fetchrow(
        "SELECT coalesce(sum(prompt_tokens),0)::bigint      AS prompt_tokens,"
        "       coalesce(sum(completion_tokens),0)::bigint  AS completion_tokens,"
        "       coalesce(sum(cached_tokens),0)::bigint      AS cached_tokens,"
        "       coalesce(sum(cost_usd),0)::float8           AS cost_usd,"
        "       count(*)::bigint                            AS calls,"
        "       bool_or(cost_estimated)                     AS any_estimated,"
        "       max(model)                                  AS model "
        "FROM builderapps.llm_usage WHERE project_id=$1", project_id)
    rows = await pool().fetch(
        "SELECT coalesce(step,'(other)') AS step, count(*)::bigint AS calls,"
        "       sum(prompt_tokens)::bigint AS prompt_tokens,"
        "       sum(completion_tokens)::bigint AS completion_tokens,"
        "       sum(cached_tokens)::bigint AS cached_tokens,"
        "       sum(cost_usd)::float8 AS cost_usd "
        "FROM builderapps.llm_usage WHERE project_id=$1 "
        "GROUP BY 1 ORDER BY sum(cost_usd) DESC LIMIT 100", project_id)
    t = dict(tot) if tot else {}
    return {
        "totals": {
            "prompt_tokens": int(t.get("prompt_tokens") or 0),
            "completion_tokens": int(t.get("completion_tokens") or 0),
            "cached_tokens": int(t.get("cached_tokens") or 0),
            "total_tokens": int((t.get("prompt_tokens") or 0) + (t.get("completion_tokens") or 0)),
            "cost_usd": round(float(t.get("cost_usd") or 0.0), 6),
            "calls": int(t.get("calls") or 0),
            "cost_estimated": bool(t.get("any_estimated")),
            "model": t.get("model") or "",
        },
        "by_step": [dict(r) for r in rows],
    }
