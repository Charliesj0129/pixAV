"""asyncpg connection pool factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

if TYPE_CHECKING:
    from pixav.config import Settings


import pgvector.asyncpg


async def init_connection(conn: asyncpg.Connection) -> None:
    await pgvector.asyncpg.register_vector(conn)


async def create_pool(settings: Settings) -> asyncpg.Pool:
    """Create and return an asyncpg connection pool."""
    pool: asyncpg.Pool = await asyncpg.create_pool(
        dsn=settings.dsn,
        min_size=2,
        max_size=10,
        init=init_connection,
    )
    return pool
