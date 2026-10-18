"""asyncpg connection pool factory."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import asyncpg

if TYPE_CHECKING:
    from pixav.config import Settings


import pgvector.asyncpg


async def init_connection(conn: asyncpg.Connection) -> None:
    await pgvector.asyncpg.register_vector(conn)


def connection_initializer(settings: Settings) -> Callable[[asyncpg.Connection], Awaitable[None]]:
    """Bind every new connection before registering codecs or exposing a pool."""
    if bool(settings.expected_db_identity) != bool(settings.expected_db_name):
        raise RuntimeError("DATABASE_INSTANCE_BINDING_INCOMPLETE")
    if settings.expected_db_identity:

        async def bound_connection(conn: asyncpg.Connection) -> None:
            row = await conn.fetchrow(
                "SELECT system_identifier::text AS identity,current_database() AS database FROM pg_control_system()"
            )
            if row is None or (row["identity"], row["database"]) != (
                settings.expected_db_identity,
                settings.expected_db_name,
            ):
                raise RuntimeError("DATABASE_INSTANCE_BINDING_CHANGED")
            await init_connection(conn)

        return bound_connection
    return init_connection


async def create_pool(settings: Settings) -> asyncpg.Pool:
    """Create and return an asyncpg connection pool."""
    pool: asyncpg.Pool = await asyncpg.create_pool(
        dsn=settings.dsn,
        min_size=2,
        max_size=10,
        init=connection_initializer(settings),
    )
    return pool
