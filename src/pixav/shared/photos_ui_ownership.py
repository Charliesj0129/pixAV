"""One Photos UI reader or writer per account and persistent guest owner."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any


def photos_ui_lock_key(account_id: object, owner: str) -> int:
    value = hashlib.sha256(f"pixav:photos-ui:{account_id}:{owner}".encode()).digest()[:8]
    return int.from_bytes(value, "big", signed=True)


@asynccontextmanager
async def photos_ui_ownership(pool: Any, account_id: object, owner: str) -> AsyncIterator[Callable[[], None]]:
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("Photos UI ownership requires a task")
    key = photos_ui_lock_key(account_id, owner)
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1::bigint)", key):
            raise RuntimeError("Photos UI is owned by another reader or writer")
        disconnected = False

        def check() -> None:
            if disconnected or conn.is_closed():
                raise RuntimeError("Photos UI database ownership lost")

        def lost_connection(_connection: Any) -> None:
            nonlocal disconnected
            disconnected = True
            task.cancel("Photos UI database ownership lost")

        conn.add_termination_listener(lost_connection)
        try:
            yield check
        finally:
            if not disconnected and not conn.is_closed():
                conn.remove_termination_listener(lost_connection)
                await conn.execute("SELECT pg_advisory_unlock($1::bigint)", key)
