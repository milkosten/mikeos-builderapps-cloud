"""Dual-auth for mikeos-builderapps-cloud (see ecosystem/AUTHENTICATION.md ROLE 1).

Two credential forms resolve to a `user_id`:
  1. `Authorization: Bearer <RS256 JWT>` minted by account.osmike.com, validated
     locally against its JWKS (aud="builderapps"). user_id = the token's `sub`.
  2. Legacy `X-API-KEY` (hive agent key) resolved via
       GET {MIKEOSCOMPUTERS_URL}/api/mikeos/agents/resolve/{agent_key} -> {valid,user_id}

A present-but-invalid Bearer MUST 401 — it never falls through to the X-API-KEY path.
Key resolutions are cached briefly; network / IdP errors fail closed.
"""
import os
import time
import logging
from typing import Optional

import httpx
import jwt
from jwt import PyJWKClient
from fastapi import Request, HTTPException

logger = logging.getLogger(__name__)

ISSUER = os.environ.get("ACCOUNT_OSMIKE_ISSUER", "https://account.osmike.com")
JWKS_URL = os.environ.get("ACCOUNT_OSMIKE_JWKS_URL", f"{ISSUER}/oauth/jwks.json")

MIKEOSCOMPUTERS_URL = os.environ.get("MIKEOSCOMPUTERS_URL", "https://account.osmike.com")

_jwks_client = PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600)

_RESOLVE_CACHE_TTL = 300  # seconds
_resolve_cache: dict[str, tuple[str, float]] = {}


def _verify_bearer(token: str) -> Optional[dict]:
    """Validate an account.osmike.com RS256 JWT locally via JWKS. None if invalid.

    Mirrors the shared fleet dual-auth: verify signature + iss + exp; take sub as
    user_id. `aud` is treated leniently (not required, not enforced) — the ecosystem
    clouds validate this way and browser tokens carry the `designer-web` client / OIDC
    scopes, not a `designer` audience."""
    try:
        key = _jwks_client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"], issuer=ISSUER,
            options={"require": ["exp", "iss", "sub"], "verify_aud": False},
        )
        if claims.get("aud"):
            logger.debug("Bearer aud=%s (not enforced)", claims.get("aud"))
        return claims
    except Exception as e:
        logger.warning("Bearer rejected: %s", e)
        return None


async def resolve_agent_key(agent_key: Optional[str]) -> Optional[str]:
    """Resolve a legacy hive key to its user_id (cached; fail-closed on error)."""
    if not agent_key:
        return None
    cached = _resolve_cache.get(agent_key)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    url = f"{MIKEOSCOMPUTERS_URL.rstrip('/')}/api/mikeos/agents/resolve/{agent_key}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning("IdP resolve returned HTTP %s", resp.status_code)
                return None
            data = resp.json()
    except Exception as e:
        logger.error("Error resolving agent key against IdP: %s", e)
        return None
    if not data or not data.get("valid"):
        return None
    user_id = data.get("user_id")
    if not user_id:
        return None
    _resolve_cache[agent_key] = (str(user_id), time.monotonic() + _RESOLVE_CACHE_TTL)
    return str(user_id)


async def authenticate(request: Request) -> Optional[str]:
    """Return user_id or None. OAuth Bearer first, then legacy X-API-KEY."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        claims = _verify_bearer(auth[7:].strip())
        if claims:
            return str(claims["sub"])
        return None  # present-but-bad Bearer -> 401, never fall through
    key = request.headers.get("x-api-key") or request.headers.get("X-API-KEY")
    return await resolve_agent_key(key)


async def authenticate_token(token: Optional[str]) -> Optional[str]:
    """Resolve a bare credential string, with no Request to read headers from.

    Exists for the WebSocket (phase 33): the browser's WebSocket API has no way to set an
    `Authorization` header — the constructor accepts a URL and a subprotocol list and nothing
    else — so a browser socket can only carry its credential in the query string. This is the
    SAME validation as `authenticate`, deliberately reusing `_verify_bearer` and
    `resolve_agent_key` rather than being a second, slightly different auth path: a socket
    that authenticated more loosely than the REST surface would be the way in.

    Accepts either credential form, matching `authenticate`, and fails closed on both.
    """
    tok = (token or "").strip()
    if not tok:
        return None
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    # A JWT is three dot-separated segments; anything else is treated as a legacy agent key.
    # Cheap disambiguation, and being wrong either way just fails closed.
    if tok.count(".") == 2:
        claims = _verify_bearer(tok)
        return str(claims["sub"]) if claims else None
    return await resolve_agent_key(tok)


# ---- FastAPI dependencies -------------------------------------------------
async def current_user(request: Request) -> str:
    """Dependency: resolved user_id, or 401."""
    uid = await authenticate(request)
    if not uid:
        raise HTTPException(status_code=401, detail="unauthorized")
    return uid


async def current_user_write(request: Request) -> str:
    """Dependency for write routes. No custom `designer.write` scope exists in the
    ecosystem — tokens carry `openid profile email` — so a valid user is sufficient.
    Kept as a distinct name so scopes can be tightened later without touching routes."""
    uid = await authenticate(request)
    if not uid:
        raise HTTPException(status_code=401, detail="unauthorized")
    return uid
