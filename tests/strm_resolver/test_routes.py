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
        assert response.json() == {"status": "ok", "module": "strm_resolver"}


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_prometheus_payload(app):
    """Strm-Resolver must honour the shared /metrics contract, not 404."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "pixav_tasks_processed_total" in response.text


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
        response = await client.get(f"/resolve/{video_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cdn_url"] == "https://lh3.googleusercontent.com/pw/NEW=dv"
    assert payload["source"] == "resolved"
    app.state.resolver.resolve.assert_awaited_once_with("https://photos.app.goo.gl/share456")
    app.state.db_pool.execute.assert_awaited_once()
    app.state.redis.setex.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_not_found(app):
    video_id = uuid.uuid4()
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = None
    app.state.redis = AsyncMock()
    app.state.redis.get.return_value = None
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


@pytest.mark.asyncio
async def test_stream_video_redirects(app):
    """Should resolve video and redirect to CDN URL."""
    video_id = uuid.uuid4()
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = {
        "id": video_id,
        "share_url": "https://share",
        "cdn_url": "https://cdn.com/video.mp4?dv",
    }
    # Cache hit
    app.state.redis = AsyncMock()
    app.state.redis.get.return_value = "https://cdn.com/video.mp4?dv"
    app.state.resolver = AsyncMock()

    transport = ASGITransport(app=app)
    # allow_redirects=False to verify the 302 itself
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get(f"/stream/{video_id}")

    assert response.status_code == 302
    assert response.headers["location"] == "https://cdn.com/video.mp4?dv"


@pytest.mark.asyncio
async def test_resolve_local_scheme_returns_local_endpoint_and_updates_db(app):
    video_id = uuid.uuid4()
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = {
        "id": video_id,
        "share_url": f"pixav-local://{video_id}",
        "cdn_url": None,
    }
    app.state.redis = AsyncMock()
    app.state.redis.get.return_value = None
    app.state.resolver = AsyncMock()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/resolve/{video_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["video_id"] == str(video_id)
    assert payload["source"] == "local"
    assert payload["cdn_url"] == f"http://test/local/{video_id}"
    app.state.resolver.resolve.assert_not_awaited()
    app.state.db_pool.execute.assert_awaited_once()
    app.state.redis.setex.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_video_serves_file(app, tmp_path):
    video_id = uuid.uuid4()
    file_path = tmp_path / "video.mp4"
    content = b"local-video-bytes"
    file_path.write_bytes(content)

    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = {
        "id": video_id,
        "local_path": str(file_path),
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/local/{video_id}")

    assert response.status_code == 200
    assert response.content == content


@pytest.mark.asyncio
async def test_local_video_honours_range_requests(app, tmp_path):
    """Players seek via Range; local streaming must answer 206 with Content-Range."""
    video_id = uuid.uuid4()
    file_path = tmp_path / "video.mp4"
    content = b"local-video-bytes"
    file_path.write_bytes(content)

    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = {
        "id": video_id,
        "local_path": str(file_path),
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/local/{video_id}", headers={"Range": "bytes=0-3"})

    assert response.status_code == 206
    assert response.content == content[:4]
    assert response.headers["content-range"] == f"bytes 0-3/{len(content)}"


@pytest.mark.asyncio
async def test_local_video_supports_suffix_range(app, tmp_path):
    video_id = uuid.uuid4()
    file_path = tmp_path / "video.mp4"
    content = b"local-video-bytes"
    file_path.write_bytes(content)
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = {"id": video_id, "local_path": str(file_path)}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/local/{video_id}", headers={"Range": "bytes=-5"})

    assert response.status_code == 206
    assert response.content == content[-5:]
    assert response.headers["content-range"] == f"bytes {len(content) - 5}-{len(content) - 1}/{len(content)}"


@pytest.mark.asyncio
async def test_local_video_rejects_unsatisfiable_range(app, tmp_path):
    video_id = uuid.uuid4()
    file_path = tmp_path / "video.mp4"
    file_path.write_bytes(b"short")
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = {"id": video_id, "local_path": str(file_path)}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/local/{video_id}", headers={"Range": "bytes=99-100"})

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */5"


@pytest.mark.asyncio
async def test_resolve_recovers_after_the_cache_expires(app):
    """The CDN URL must be re-resolved once its Redis entry expires.

    Regression: the resolver used to read a persisted videos.cdn_url on a cache
    miss. That column had no expiry, so once a Google Photos signed URL died the
    resolver kept serving it and refreshing the cache with it — permanently, and
    without self-repair. share_url is the durable fact; the CDN URL is derived.
    """
    video_id = uuid.uuid4()
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = {
        "id": video_id,
        "share_url": "https://photos.app.goo.gl/share123",
    }
    redis = AsyncMock()
    redis.get.return_value = None  # cache expired
    app.state.redis = redis
    resolver = AsyncMock()
    resolver.resolve.return_value = "https://lh3.googleusercontent.com/pw/FRESH=dv"
    app.state.resolver = resolver

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/resolve/{video_id}")

    payload = response.json()
    assert payload["source"] == "resolved"
    assert payload["cdn_url"] == "https://lh3.googleusercontent.com/pw/FRESH=dv"
    resolver.resolve.assert_awaited_once_with("https://photos.app.goo.gl/share123")
    redis.setex.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolver_never_reads_a_persisted_cdn_url(app):
    """Even if a stale column were present, it must not short-circuit resolution."""
    video_id = uuid.uuid4()
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchrow.return_value = {
        "id": video_id,
        "share_url": "https://photos.app.goo.gl/share123",
        "cdn_url": "https://lh3.googleusercontent.com/pw/EXPIRED=dv",
    }
    app.state.redis = AsyncMock()
    app.state.redis.get.return_value = None
    resolver = AsyncMock()
    resolver.resolve.return_value = "https://lh3.googleusercontent.com/pw/FRESH=dv"
    app.state.resolver = resolver

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/resolve/{video_id}")

    assert response.json()["cdn_url"] == "https://lh3.googleusercontent.com/pw/FRESH=dv"

    # The SELECT itself must not ask for the dropped column.
    query = app.state.db_pool.fetchrow.await_args.args[0]
    assert "cdn_url" not in query
