"""Tests for strm_resolver routes."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from pixav.strm_resolver.app import create_app, lifespan


@pytest.mark.asyncio
async def test_reader_role_refusal_closes_startup_resources():
    app = create_app(redis_url=None, db_dsn="postgresql://synthetic")
    app.state.require_reader_role = True
    pool = AsyncMock()
    with (
        patch("pixav.strm_resolver.app._open_database", return_value=pool),
        patch("pixav.strm_resolver.app.require_workflow_role", side_effect=RuntimeError("dedicated role required")),
    ):
        with pytest.raises(RuntimeError, match="dedicated"):
            async with lifespan(app):
                pytest.fail("a privileged resolver must never start")
    pool.close.assert_awaited_once()
    assert app.state.db_pool is None


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


@pytest.mark.parametrize("failure", [None, ConnectionError("dependency unavailable"), TimeoutError()])
async def test_configured_health_rejects_unreadable_database(app, failure):
    app.state.db_dsn = "configured-private-dsn"
    if failure is not None:
        app.state.db_pool = AsyncMock()
        app.state.db_pool.fetchval.side_effect = failure
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}


async def test_configured_health_checks_database_before_reporting_ready(app):
    app.state.db_dsn = "configured-private-dsn"
    app.state.db_pool = AsyncMock()
    app.state.db_pool.fetchval.return_value = 1
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health")).status_code == 200
    app.state.db_pool.fetchval.assert_awaited_once_with("SELECT 1")


async def test_failed_database_startup_exits_and_closes_optional_redis(app, caplog):
    app.state.db_dsn = "configured-private-dsn"
    app.state.redis_url = "configured-private-redis"
    redis = AsyncMock()
    with (
        patch("pixav.strm_resolver.app.aioredis.from_url", return_value=redis),
        patch("pixav.strm_resolver.app.asyncpg.create_pool", side_effect=ConnectionError("private connection detail")),
    ):
        with pytest.raises(RuntimeError, match="PostgreSQL is required"):
            async with lifespan(app):
                pytest.fail("uninitialized playback must not become ready")
    redis.aclose.assert_awaited_once()
    assert app.state.db_pool is None
    assert "private connection detail" not in caplog.text


async def test_resolver_checks_database_identity_before_initializing_each_connection(monkeypatch):
    from pixav.config import Settings

    settings = Settings(expected_db_identity="123", expected_db_name="synthetic_database")
    monkeypatch.setattr("pixav.strm_resolver.app.get_settings", lambda: settings)
    app = create_app(redis_url=None, db_dsn="postgresql://synthetic")
    pool = AsyncMock()
    captured = {}

    async def connect(**kwargs):
        captured.update(kwargs)
        return pool

    with patch("pixav.strm_resolver.app.asyncpg.create_pool", side_effect=connect):
        async with lifespan(app):
            pass
    initializer = captured.get("init")
    assert callable(initializer), "resolver omitted the managed database identity initializer"
    connection = AsyncMock()
    connection.fetchrow.return_value = {"identity": "different-cluster", "database": "synthetic_database"}
    with pytest.raises(RuntimeError, match="DATABASE_INSTANCE_BINDING_CHANGED"):
        await initializer(connection)


async def test_resolver_refuses_mismatched_bound_redis_before_opening_database(monkeypatch, caplog):
    from pixav.config import Settings

    settings = Settings(expected_redis_run_id="a" * 40)
    monkeypatch.setattr("pixav.strm_resolver.app.get_settings", lambda: settings)
    app = create_app(redis_url="redis://synthetic", db_dsn="postgresql://synthetic")
    redis = AsyncMock()
    redis.info.return_value = {"run_id": "b" * 40}
    with (
        patch("pixav.strm_resolver.app.aioredis.from_url", return_value=redis),
        patch("pixav.strm_resolver.app._open_database", return_value=AsyncMock()) as database,
    ):
        with pytest.raises(RuntimeError, match="Redis instance binding required"):
            async with lifespan(app):
                pytest.fail("resolver must not accept a different Redis instance")
    database.assert_not_awaited()
    redis.aclose.assert_awaited_once()
    assert "redis://synthetic" not in caplog.text


async def test_resolver_refuses_database_drift_with_only_database_binding(monkeypatch, caplog):
    from pixav.config import Settings

    settings = Settings(expected_redis_database=1)
    monkeypatch.setattr("pixav.strm_resolver.app.get_settings", lambda: settings)
    app = create_app(redis_url="redis://synthetic/0", db_dsn="postgresql://synthetic")
    redis = AsyncMock()
    redis.connection_pool.connection_kwargs = {"db": 0}
    with (
        patch("pixav.strm_resolver.app.aioredis.from_url", return_value=redis),
        patch("pixav.strm_resolver.app._open_database", return_value=AsyncMock()) as database,
    ):
        with pytest.raises(RuntimeError, match="Redis instance binding required"):
            async with lifespan(app):
                pytest.fail("resolver must not ignore a different Redis database")
    database.assert_not_awaited()
    redis.info.assert_not_awaited()
    redis.aclose.assert_awaited_once()
    assert "redis://synthetic/0" not in caplog.text


@pytest.mark.parametrize(
    "field",
    [
        "resolver_auth_required",
        "resolver_require_reader_role",
        "resolver_token_file",
        "expected_db_identity",
        "expected_db_name",
        "expected_redis_run_id",
    ],
)
def test_managed_resolver_cannot_start_with_public_or_unbound_configuration(monkeypatch, tmp_path, field):
    from pixav.config import Settings

    settings = Settings(
        managed_media_workflow=True,
        resolver_auth_required=True,
        resolver_require_reader_role=True,
        resolver_token_file=str(tmp_path / "token"),
        expected_db_identity="123",
        expected_db_name="synthetic_database",
        expected_redis_run_id="a" * 40,
        playback_cache_dir=str(tmp_path / "cache"),
    )
    monkeypatch.setattr("pixav.strm_resolver.app.get_settings", lambda: settings)
    assert create_app() is not None
    settings = settings.model_copy(update={field: False if field.endswith(("required", "role")) else ""})
    with pytest.raises(RuntimeError, match="Managed playback requires"):
        create_app()


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
