"""asyncpg pool + idempotent migration runner.

Applies migrations/*.sql on boot, tracked in a `_migrations` table so each file
runs once. Every migration is itself idempotent (IF NOT EXISTS) as a belt-and-braces.
"""
import os
import logging
from pathlib import Path
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
        await _run_migrations(_pool)
    return _pool


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("pool not initialized")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def _run_migrations(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations ("
            " filename text primary key, applied_at timestamptz default now())"
        )
        if not MIGRATIONS_DIR.is_dir():
            logger.warning("migrations dir %s missing", MIGRATIONS_DIR)
            return
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            done = await conn.fetchval(
                "SELECT 1 FROM _migrations WHERE filename = $1", path.name
            )
            if done:
                continue
            sql = path.read_text()
            logger.info("applying migration %s", path.name)
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO _migrations(filename) VALUES ($1) "
                    "ON CONFLICT (filename) DO NOTHING",
                    path.name,
                )
