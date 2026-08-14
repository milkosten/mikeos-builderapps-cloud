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
    return dict(row) if row else None


async def list_projects(user_id: str) -> list[dict]:
    rows = await pool().fetch(
        "SELECT id,title,status,subdomain,pipeline,created_at,updated_at "
        "FROM builderapps.projects WHERE user_id=$1 ORDER BY created_at DESC LIMIT 200",
        user_id,
    )
    return [dict(r) for r in rows]


# ---- project_secrets ------------------------------------------------------
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
                            compose_hash: str = "") -> int:
    dep_id = await pool().fetchval(
        "INSERT INTO builderapps.deployments(project_id,image_tag,compose_hash,status) "
        "VALUES ($1,$2,$3,'deploying') RETURNING id",
        project_id, image_tag, compose_hash,
    )
    if dep_id is None:
        raise RuntimeError("create_deployment returned no id")
    return int(dep_id)


async def list_deployments(project_id: str, limit: int = 50) -> list[dict]:
    rows = await pool().fetch(
        "SELECT id,image_tag,status,health,started_at,finished_at "
        "FROM builderapps.deployments WHERE project_id=$1 "
        "ORDER BY started_at DESC LIMIT $2",
        project_id, max(1, min(int(limit), 200)),
    )
    return [dict(r) for r in rows]


async def finish_deployment(dep_id: int, status: str, health: str = "") -> None:
    res = await pool().execute(
        "UPDATE builderapps.deployments SET status=$2, health=$3, finished_at=now() "
        "WHERE id=$1",
        dep_id, status, health[:4000],
    )
    if res != "UPDATE 1":
        raise RuntimeError(f"finish_deployment affected {res!r} for {dep_id}")


# ---- pipeline runs / steps ------------------------------------------------
async def create_run(project_id: str, kind: str, request: str, total_steps: int) -> int:
    run_id = await pool().fetchval(
        "INSERT INTO builderapps.pipeline_runs(project_id,kind,request,total_steps) "
        "VALUES ($1,$2,$3,$4) RETURNING id",
        project_id, kind, request, total_steps,
    )
    if run_id is None:
        raise RuntimeError("create_run returned no id")
    return int(run_id)


async def set_run_total(run_id: int, total_steps: int) -> None:
    await pool().execute(
        "UPDATE builderapps.pipeline_runs SET total_steps=$2 WHERE id=$1",
        run_id, total_steps,
    )


async def finish_run(run_id: int, status: str) -> None:
    await pool().execute(
        "UPDATE builderapps.pipeline_runs SET status=$2, finished_at=now() WHERE id=$1",
        run_id, status,
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
        await pool().execute(
            "UPDATE builderapps.pipeline_runs SET current_step=$2 WHERE id=$1",
            run_id, idx,
        )


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
    """{"run_id": int|None, "steps": [...]} for the project's most recent run (ordered by
    idx). Empty list when the project has no run yet — never an error."""
    run = await pool().fetchrow(
        "SELECT id FROM builderapps.pipeline_runs WHERE project_id=$1 "
        "ORDER BY created_at DESC LIMIT 1", project_id,
    )
    if not run:
        return {"run_id": None, "steps": []}
    rows = await pool().fetch(
        "SELECT idx,name,status,log,ts FROM builderapps.pipeline_steps "
        "WHERE run_id=$1 ORDER BY idx", int(run["id"]),
    )
    return {"run_id": int(run["id"]), "steps": [dict(r) for r in rows]}
