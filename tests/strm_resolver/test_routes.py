"""Tests for strm_resolver routes."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from pixav.strm_resolver.app import create_app


@pytest.fixture
def app():
    """Create a test FastAPI application."""
    return create_app(redis_url=None, db_dsn=None)


@pytest.mark.asyncio
async def test_health_returns_ok(app):
    """Test that health endpoint returns 200 with status ok."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_resolve_cache_hit(app):
    """Should return cached CDN URL without calling external resolver."""
    video_id = uuid.uuid4()
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = {
        "id": video_id,
        "share_url": "https://photos.app.goo.gl/share123",
        "cdn_url": None,
    }
    redis = AsyncMock()
    redis.get.return_value = "https://lh3.googleusercontent.com/pw/CACHED=dv"
    app.state.redis = redis
    app.state.resolver = AsyncMock()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/resolve/{video_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["video_id"] == str(video_id)
    assert payload["source"] == "cache"
    assert payload["cdn_url"] == "https://lh3.googleusercontent.com/pw/CACHED=dv"
    app.state.resolver.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_cache_miss_resolves_and_updates_db(app):
    """Should resolve share URL, persist CDN URL, and cache it."""
    video_id = uuid.uuid4()
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = {
        "id": video_id,
        "share_url": "https://photos.app.goo.gl/share456",
        "cdn_url": None,
    }
    app.state.redis = AsyncMock()
    app.state.redis.get.return_value = None
    app.state.resolver = AsyncMock()
    app.state.resolver.resolve.return_value = "https://lh3.googleusercontent.com/pw/NEW=dv"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/stream/{video_id}")

    assert response.status_code == 302
    assert response.headers["location"] == "https://lh3.googleusercontent.com/pw/NEW=dv"
    app.state.resolver.resolve.assert_awaited_once_with("https://photos.app.goo.gl/share456")
    app.state.db_pool.execute.assert_awaited_once()
    app.state.redis.setex.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_not_found(app):
    video_id = uuid.uuid4()
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = None
    app.state.redis = AsyncMock()
    app.state.resolver = AsyncMock()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/resolve/{video_id}")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_resolve_invalid_uuid_returns_400(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/resolve/not-a-uuid")

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_resolve_missing_share_url_returns_409(app):
    video_id = uuid.uuid4()
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = {
        "id": video_id,
        "share_url": None,
        "cdn_url": None,
    }
    app.state.redis = AsyncMock()
    app.state.redis.get.return_value = None
    app.state.resolver = AsyncMock()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/resolve/{video_id}")

    assert response.status_code == 409
