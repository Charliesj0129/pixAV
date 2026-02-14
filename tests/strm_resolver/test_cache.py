"""Tests for CdnCache."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pixav.strm_resolver.cache import CdnCache


@pytest.fixture
def mock_redis() -> AsyncMock:
    r = AsyncMock()
    r.get.return_value = None
    r.setex.return_value = True
    return r


@pytest.fixture
def cache(mock_redis: AsyncMock) -> CdnCache:
    return CdnCache(redis=mock_redis, ttl=3300)


class TestCdnCache:
    async def test_get_miss(self, cache: CdnCache, mock_redis: AsyncMock) -> None:
        mock_redis.get.return_value = None
        result = await cache.get("video-1")
        assert result is None
        mock_redis.get.assert_awaited_once_with("pixav:cdn:video-1")

    async def test_get_hit(self, cache: CdnCache, mock_redis: AsyncMock) -> None:
        mock_redis.get.return_value = "https://lh3.googleusercontent.com/pw/ABC=dv"
        result = await cache.get("video-2")
        assert result == "https://lh3.googleusercontent.com/pw/ABC=dv"

    async def test_set(self, cache: CdnCache, mock_redis: AsyncMock) -> None:
        await cache.set("video-3", "https://cdn.example.com/v3")
        mock_redis.setex.assert_awaited_once_with("pixav:cdn:video-3", 3300, "https://cdn.example.com/v3")

    async def test_custom_ttl(self, mock_redis: AsyncMock) -> None:
        cache = CdnCache(redis=mock_redis, ttl=60)
        await cache.set("vid", "url")
        mock_redis.setex.assert_awaited_once_with("pixav:cdn:vid", 60, "url")
