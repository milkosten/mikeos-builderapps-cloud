"""Gitea integration (phase 09).

Thin wrapper over the self-hosted Gitea at GITEA_URL using the admin token. Creates
per-user accounts (idempotent), mints per-user tokens (stored encrypted via store), and
generates project repos from the `templates/nodejs-fullstack` template. Checkout uses a
token-over-HTTPS clone URL (no SSH plumbing).

Never trust HTTP 200 — every create is verified by a follow-up GET.
"""
import logging
import os
import re
import secrets
from typing import Optional

import httpx

from server import store

logger = logging.getLogger(__name__)

GITEA_URL = os.environ.get("GITEA_URL", "https://gitea.osmike.com").rstrip("/")
GITEA_API = f"{GITEA_URL}/api/v1"
GITEA_ADMIN_USER = os.environ.get("GITEA_ADMIN_USER", "").strip()
GITEA_ADMIN_TOKEN = os.environ.get("GITEA_ADMIN_TOKEN", "").strip()
GITEA_ADMIN_PW = os.environ.get("GITEA_ADMIN_PW", "").strip()

TEMPLATE_OWNER = os.environ.get("GITEA_TEMPLATE_OWNER", "templates")
TEMPLATE_REPO = os.environ.get("GITEA_TEMPLATE_REPO", "nodejs-fullstack")

_TIMEOUT = 30.0


def _admin_headers() -> dict:
    return {"Authorization": f"token {GITEA_ADMIN_TOKEN}",
            "Content-Type": "application/json"}


def gitea_username_for(user_id: str) -> str:
    """Stable, collision-free Gitea username derived from the immutable MikeOS user_id.

    Gitea usernames: alnum + dash/underscore/dot, must start alnum. We use `u<slug>`
    where slug is the sanitized user_id, so it's deterministic and unique per user."""
    slug = re.sub(r"[^a-zA-Z0-9]", "", user_id).lower()[:30] or "user"
    return f"u{slug}"


async def _get_user(username: str) -> Optional[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(f"{GITEA_API}/users/{username}", headers=_admin_headers())
        if r.status_code == 200:
            return r.json()
        return None


async def ensure_user(user_id: str, email: Optional[str]) -> dict:
    """Idempotent + self-healing: return {gitea_username, token}.

    If a gitea_accounts row exists AND the Gitea user still exists, reuse it. Otherwise
    create the Gitea user (swallow 'already exists'), mint a fresh per-user token, and
    persist it encrypted."""
    username = gitea_username_for(user_id)

    existing = await store.get_gitea_account(user_id)
    if existing:
        # self-heal: if Gitea was reset the user may 404 — recreate below.
        if await _get_user(existing["gitea_username"]):
            return {"gitea_username": existing["gitea_username"], "token": existing["token"]}
        logger.warning("gitea user %s missing though row exists — recreating", existing["gitea_username"])
        username = existing["gitea_username"]

    # Create the user (admin). Idempotent: a 422/409 "already exists" is fine.
    password = secrets.token_urlsafe(24)
    safe_email = email if (email and "@" in email) else f"{username}@builderapps.local"
    body = {
        "username": username,
        "email": safe_email,
        "password": password,
        "must_change_password": False,
        "send_notify": False,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{GITEA_API}/admin/users", headers=_admin_headers(), json=body)
        if r.status_code not in (201, 200):
            txt = r.text.lower()
            if r.status_code in (409, 422) and ("exist" in txt or "already" in txt):
                logger.info("gitea user %s already exists", username)
            else:
                raise RuntimeError(f"gitea create user failed HTTP {r.status_code}: {r.text[:300]}")

    # verify the user exists (never trust 200)
    if not await _get_user(username):
        raise RuntimeError(f"gitea user {username} not present after create")

    # Mint a per-user token via the admin sudo header (admin acts as the user).
    token = await _mint_user_token(username)

    await store.upsert_gitea_account(user_id, username, token)
    return {"gitea_username": username, "token": token}


async def _mint_user_token(username: str) -> str:
    """Create a per-user token. Gitea's /users/{u}/tokens requires BASIC auth (not a token);
    the admin acts on the user's behalf via the `Sudo` header + admin basic credentials."""
    if not GITEA_ADMIN_PW:
        raise RuntimeError("GITEA_ADMIN_PW is required to mint per-user Gitea tokens")
    name = f"builderapps-{secrets.token_hex(4)}"
    headers = {"Sudo": username, "Content-Type": "application/json"}
    body = {"name": name, "scopes": ["write:repository", "write:user"]}
    async with httpx.AsyncClient(timeout=_TIMEOUT,
                                 auth=(GITEA_ADMIN_USER, GITEA_ADMIN_PW)) as c:
        r = await c.post(f"{GITEA_API}/users/{username}/tokens", headers=headers, json=body)
        if r.status_code not in (201, 200):
            raise RuntimeError(f"gitea mint token failed HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
    tok = data.get("sha1") or data.get("token")
    if not tok:
        raise RuntimeError("gitea token response had no sha1")
    return tok


async def create_project_repo(gitea_user: str, shortid: str) -> dict:
    """Generate `app-<shortid>` in the user's namespace from the template. Idempotent-ish:
    if the repo already exists, return it. Verified by a follow-up GET."""
    repo_name = f"app-{shortid}"
    # Gitea requires at least one template item flag to be true, else 422.
    body = {"owner": gitea_user, "name": repo_name, "private": True,
            "description": f"builderapps project {shortid}", "default_branch": "main",
            "git_content": True, "git_hooks": False, "webhooks": False, "topics": True,
            "avatar": False, "labels": False, "protected_branch": False}
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(
            f"{GITEA_API}/repos/{TEMPLATE_OWNER}/{TEMPLATE_REPO}/generate",
            headers=_admin_headers(), json=body,
        )
        if r.status_code not in (201, 200):
            txt = r.text.lower()
            if r.status_code == 409 or "exist" in txt:
                logger.info("repo %s/%s already exists", gitea_user, repo_name)
            else:
                raise RuntimeError(f"gitea generate repo failed HTTP {r.status_code}: {r.text[:300]}")
        # verify
        rr = await c.get(f"{GITEA_API}/repos/{gitea_user}/{repo_name}", headers=_admin_headers())
        if rr.status_code != 200:
            raise RuntimeError(f"repo {gitea_user}/{repo_name} not present after generate")
        info = rr.json()
    return {"owner": gitea_user, "repo": repo_name,
            "full_name": info.get("full_name"),
            "clone_url": info.get("clone_url"),
            "default_branch": info.get("default_branch") or "main"}


def clone_url_for(gitea_user: str, repo: str, token: str) -> str:
    """Token-over-HTTPS clone URL (no SSH). Token is URL-safe already."""
    base = GITEA_URL.split("://", 1)[-1]
    scheme = GITEA_URL.split("://", 1)[0]
    return f"{scheme}://{gitea_user}:{token}@{base}/{gitea_user}/{repo}.git"
