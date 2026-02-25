"""Tests for strm_resolver middleware behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from pixav.strm_resolver.app import create_app


@pytest.mark.asyncio
async def test_rate_limit_blocks_when_rpm_exceeded() -> None:
    app = create_app(redis_url=None, db_dsn=None)
    redis = AsyncMock()
    redis.incr.return_value = 999
    app.state.redis = redis

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 429
    body = response.json()
    assert body["detail"] == "Too many requests"


@pytest.mark.asyncio
async def test_rate_limit_allows_when_redis_unavailable() -> None:
    app = create_app(redis_url=None, db_dsn=None)
    app.state.redis = None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
