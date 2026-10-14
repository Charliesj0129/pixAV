"""One Photos UI reader or writer per account and persistent runtime owner.

Connection locks do not fence effects after a disconnect. An external runtime
must also retain a persistent fence until its previous writers are stopped.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any


class PhotosUIConnection:
    """An advisory guard and the connection holding its resource locks."""

    def __init__(self, connection: Any, check: Callable[[], None]) -> None:
        self.connection, self._check = connection, check

    def __call__(self) -> None:
        self._check()

    def connected(self) -> bool:
        try:
            self._check()
        except Exception:
            # asyncpg may release the proxy during its termination callback.
            return False
        return True


def photos_ui_lock_key(account_id: object, owner: str) -> int:
    value = hashlib.sha256(f"pixav:photos-ui:{account_id}:{owner}".encode()).digest()[:8]
    return int.from_bytes(value, "big", signed=True)


def _resource_lock_key(kind: str, identity: object) -> int:
    value = hashlib.sha256(f"pixav:photos-ui-resource:{kind}:{identity}".encode()).digest()[:8]
    return int.from_bytes(value, "big", signed=True)


async def _require_unfenced(conn: Any, account_id: object, owner: str) -> None:
    if await conn.fetchval("SELECT to_regclass('public.photos_ui_sessions') IS NOT NULL"):
        blocked = await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM photos_ui_sessions
            WHERE state IN ('ACTIVE','HALT_REQUIRED')
            AND (account_id::text=$1 OR runtime_owner::text=$2))""",
            str(account_id),
            owner,
        )
        if blocked:
            raise RuntimeError("Photos UI remains fenced until verified writer halt")


@asynccontextmanager
async def photos_ui_ownership(pool: Any, account_id: object, owner: str) -> AsyncIterator[PhotosUIConnection]:
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("Photos UI ownership requires a task")
    keys = sorted(
        {
            photos_ui_lock_key(account_id, owner),
            _resource_lock_key("account", account_id),
            _resource_lock_key("owner", owner),
        }
    )
    async with pool.acquire() as conn:
        acquired = []
        disconnected = False
        listening = False

        def check() -> None:
            if disconnected or conn.is_closed():
                raise RuntimeError("Photos UI database ownership lost")

        def lost_connection(_connection: Any) -> None:
            nonlocal disconnected
            disconnected = True
            task.cancel("Photos UI database ownership lost")

        try:
            for key in keys:
                if not await conn.fetchval("SELECT pg_try_advisory_lock($1::bigint)", key):
                    raise RuntimeError("Photos UI is owned by another reader or writer")
                acquired.append(key)
            conn.add_termination_listener(lost_connection)
            listening = True
            await _require_unfenced(conn, account_id, owner)
            yield PhotosUIConnection(conn, check)
        finally:
            if not disconnected and not conn.is_closed():
                if listening:
                    conn.remove_termination_listener(lost_connection)
                for key in reversed(acquired):
                    await conn.execute("SELECT pg_advisory_unlock($1::bigint)", key)
