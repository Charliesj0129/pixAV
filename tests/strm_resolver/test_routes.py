"""Tests for strm_resolver routes."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pixav.strm_resolver.app import create_app


@pytest.fixture
def app():
    """Create a test FastAPI application."""
    return create_app(redis_url=None)


@pytest.mark.asyncio
async def test_health_returns_ok(app):
    """Test that health endpoint returns 200 with status ok."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_resolve_returns_501_stub(app):
    """Test that resolve endpoint returns 501 (not implemented)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        video_id = "test-video-123"
        response = await client.get(f"/resolve/{video_id}")
        assert response.status_code == 501
        assert "not yet implemented" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_stream_returns_501_stub(app):
    """Test that stream endpoint returns 501 (not implemented)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        video_id = "test-video-456"
        response = await client.get(f"/stream/{video_id}")
        assert response.status_code == 501
        assert "not yet implemented" in response.json()["detail"].lower()
