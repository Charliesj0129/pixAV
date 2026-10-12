"""One managed authority per PostgreSQL database, held for the process lifetime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

AUTHORITY_LOCK = 716492805322


@asynccontextmanager
async def execution_authority_ownership(pool: Any) -> AsyncIterator[Any]:
    """Refuse competing startup; cancel the scheduling task if ownership is lost."""
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("execution authority requires an asyncio task")
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1::bigint)", AUTHORITY_LOCK):
            raise RuntimeError("another execution authority owns this database")
        disconnected = False

        def lost_connection(_connection: Any) -> None:
            nonlocal disconnected
            disconnected = True
            task.cancel("execution authority database ownership lost")

        conn.add_termination_listener(lost_connection)
        try:
            yield conn
        finally:
            if not disconnected and not conn.is_closed():
                conn.remove_termination_listener(lost_connection)
                await conn.execute("SELECT pg_advisory_unlock($1::bigint)", AUTHORITY_LOCK)
