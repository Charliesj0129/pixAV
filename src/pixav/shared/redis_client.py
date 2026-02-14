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
    return client
