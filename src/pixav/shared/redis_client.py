"""Redis connection wrapper."""

from __future__ import annotations

from typing import TYPE_CHECKING

import redis.asyncio as aioredis

if TYPE_CHECKING:
    from pixav.config import Settings


async def create_redis(settings: Settings) -> aioredis.Redis:
    """Create and return an async Redis client."""
    client: aioredis.Redis = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
    )
    try:
        if settings.expected_redis_database is not None:
            selected = int(client.connection_pool.connection_kwargs.get("db", 0))
            if selected != settings.expected_redis_database:
                raise RuntimeError("REDIS_DATABASE_BINDING_CHANGED")
        if settings.expected_redis_run_id:
            observed = str((await client.info("server")).get("run_id", ""))
            if observed != settings.expected_redis_run_id:
                raise RuntimeError("REDIS_INSTANCE_BINDING_CHANGED")
    except BaseException:
        await client.aclose()
        raise
    return client
