"""Redis TTL cache for CDN URLs."""

from __future__ import annotations

from redis import asyncio as aioredis


class CdnCache:
    """Redis-backed cache for CDN URLs with TTL."""

    def __init__(self, redis: aioredis.Redis, ttl: int = 3300) -> None:
        """Initialize the CDN cache.

        Args:
            redis: Redis client instance
            ttl: Time-to-live in seconds (default: 3300 = 55 minutes)
        """
        self.redis = redis
        self.ttl = ttl
        self.key_prefix = "pixav:cdn:"

    async def get(self, video_id: str) -> str | None:
        """Retrieve a cached CDN URL.

        Args:
            video_id: The video identifier

        Returns:
            Cached CDN URL or None if not found
        """
        key = f"{self.key_prefix}{video_id}"
        return await self.redis.get(key)

    async def set(self, video_id: str, cdn_url: str) -> None:
        """Store a CDN URL in cache with TTL.

        Args:
            video_id: The video identifier
            cdn_url: The CDN URL to cache
        """
        key = f"{self.key_prefix}{video_id}"
        await self.redis.setex(key, self.ttl, cdn_url)
