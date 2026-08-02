"""asyncpg connection pool lifecycle.

The app talks to Supabase's Postgres directly over `DATABASE_URL` with
asyncpg — a thin, honest choice: the data is relational, the queries are
plain SQL, and asyncpg is natively async so nothing blocks the event loop.
The pool is created in the FastAPI lifespan (main.py) and shared process-
wide; repository functions borrow a connection from it per query.
"""

from __future__ import annotations

import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — call init_pool() in the app lifespan")
    return _pool
