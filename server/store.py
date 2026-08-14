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


async def latest_run_for(project_id: str) -> Optional[dict]:
    run = await pool().fetchrow(
        "SELECT id FROM builderapps.pipeline_runs WHERE project_id=$1 "
        "ORDER BY created_at DESC LIMIT 1", project_id,
    )
    return await get_run_with_steps(int(run["id"])) if run else None
