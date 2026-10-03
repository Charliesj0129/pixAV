"""Stable managed stream endpoint (BDD-064, 065, 067-078, 081, 082).

The database is a fake that answers the eligibility query and records demand;
the cache is a real directory, so locking, naming, size checks and byte ranges
run for real.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from unittest.mock import AsyncMock

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from pixav.shared import metrics
from pixav.shared.playback_models import cache_object_path
from pixav.strm_resolver import playback
from pixav.strm_resolver.app import create_app
from pixav.strm_resolver.movie_acceptance import stable_acceptance
from pixav.strm_resolver.playback import PlaybackCache

CONTENT = bytes(range(256)) * 1024  # 256 KiB of synthetic, position-dependent bytes
SIZE = len(CONTENT)


class FakePool:
    """Answers only the managed queries; anything else is a test failure."""

    def __init__(self, *, managed: bool = True, ready: bool = True) -> None:
        self.playable_id = uuid.uuid4()
        self.managed, self.ready = managed, ready
        self.demands: list[uuid.UUID] = []
        self.on_demand = None

    async def fetchrow(self, sql: str, *args):
        assert "AS managed" in sql, sql
        return {
            "managed": self.managed,
            "playable_asset_id": self.playable_id if self.ready else None,
            "size_bytes": SIZE if self.ready else None,
        }

    async def execute(self, sql: str, *args):
        assert "playback_cache_requests" in sql, sql
        self.demands.append(args[0])
        if self.on_demand is not None:
            self.on_demand()
        return "INSERT 0 1"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(playback, "_POLL_SECONDS", 0.01)
    application = create_app(redis_url=None, db_dsn=None)
    application.state.playback_cache = PlaybackCache(tmp_path)
    application.state.playback_cold_wait_seconds = 0
    application.state.db_pool = FakePool()
    application.state.resolver = AsyncMock()
    application.state.redis = AsyncMock()
    application.state.redis.get.return_value = "https://lh3.googleusercontent.com/pw/STALE=dv"
    return application


def publish(app, content: bytes = CONTENT):
    path = cache_object_path(app.state.playback_cache.root, app.state.db_pool.playable_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


async def request(app, method: str, video_id: uuid.UUID | None = None, headers: dict | None = None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, f"/stream/{video_id or uuid.uuid4()}", headers=headers or {})


def outcome(name: str) -> float:
    return metrics.playback_requests.labels(outcome=name)._value.get()


async def test_full_get_returns_the_verified_object_bdd_068_069(app):
    publish(app)
    before = outcome("hit")

    response = await request(app, "GET")

    assert response.status_code == 200 and response.headers["content-type"] == "video/mp4"
    assert int(response.headers["content-length"]) == SIZE
    assert hashlib.sha256(response.content).hexdigest() == hashlib.sha256(CONTENT).hexdigest()
    assert outcome("hit") == before + 1


@pytest.mark.parametrize(
    ("header", "start", "end"),
    [
        ("bytes=0-65535", 0, 65535),  # BDD-072
        (f"bytes={SIZE // 2}-{SIZE // 2 + 4095}", SIZE // 2, SIZE // 2 + 4095),  # BDD-073
        ("bytes=-65536", SIZE - 65536, SIZE - 1),  # BDD-074
        (f"bytes={SIZE - 10}-", SIZE - 10, SIZE - 1),
    ],
)
async def test_ranges_return_exactly_the_requested_bytes_bdd_072_074(app, header, start, end):
    publish(app)

    response = await request(app, "GET", headers={"Range": header})

    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes {start}-{end}/{SIZE}"
    assert response.content == CONTENT[start : end + 1]


async def test_seek_reads_only_from_the_requested_offset_bdd_073_076(app, monkeypatch):
    publish(app)
    offsets: list[int] = []
    real_pread = os.pread

    def spy(descriptor, length, offset):
        offsets.append(offset)
        return real_pread(descriptor, length, offset)

    monkeypatch.setattr(playback.os, "pread", spy)
    middle = SIZE // 2

    response = await request(app, "GET", headers={"Range": f"bytes={middle}-{middle + 999}"})

    assert response.status_code == 206 and len(response.content) == 1000
    assert min(offsets) == middle


async def test_head_answers_from_the_ready_record_without_the_cache_bdd_070_071(app):
    """No object is published: HEAD must neither open it nor ask for a rebuild."""
    response = await request(app, "HEAD")

    assert response.status_code == 200 and response.content == b""
    assert int(response.headers["content-length"]) == SIZE
    assert response.headers["accept-ranges"] == "bytes"
    assert app.state.db_pool.demands == []


async def test_out_of_range_is_416_without_touching_the_cache_bdd_075(app):
    response = await request(app, "GET", headers={"Range": f"bytes={SIZE}-"})

    assert response.status_code == 416 and response.headers["content-range"] == f"bytes */{SIZE}"
    assert app.state.db_pool.demands == []


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_not_ready_managed_video_is_refused_bdd_067_082(app, method):
    """A first segment, a PREPARING object, or a copy that is not DURABLE never plays."""
    app.state.db_pool.ready = False
    publish(app, CONTENT[:1024])  # whatever bytes exist locally are irrelevant

    response = await request(app, method)

    assert response.status_code == 409 and CONTENT[:1024] not in response.content
    assert app.state.db_pool.demands == []


async def test_managed_video_never_uses_a_provider_url_bdd_065(app):
    """A stale CDN entry in Redis cannot reach a managed video's stable URL."""
    publish(app)

    response = await request(app, "GET")

    assert response.status_code == 200
    app.state.redis.get.assert_not_awaited()
    app.state.resolver.resolve.assert_not_awaited()


async def test_resolve_returns_only_the_stable_url_for_a_managed_video_bdd_064(app):
    video_id = uuid.uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/resolve/{video_id}")

    assert response.json() == {
        "video_id": str(video_id),
        "stream_url": f"http://test/stream/{video_id}",
        "source": "managed",
        "state": "READY",
    }
    assert "googleusercontent" not in response.text
    app.state.resolver.resolve.assert_not_awaited()


async def test_cold_miss_waits_for_the_rebuild_and_serves_it_bdd_078_081(app):
    pool = app.state.db_pool
    app.state.playback_cold_wait_seconds = 5
    pool.on_demand = lambda: asyncio.get_running_loop().call_later(0.05, publish, app)
    before = outcome("cold_hit")

    response = await request(app, "GET", headers={"Range": "bytes=0-99"})

    assert response.status_code == 206 and response.content == CONTENT[:100]
    assert pool.demands == [pool.playable_id]
    assert outcome("cold_hit") == before + 1


async def test_cold_miss_past_the_wait_is_503_and_keeps_the_demand_bdd_078(app):
    before = outcome("cold_timeout")

    response = await request(app, "GET")

    assert response.status_code == 503 and response.headers["retry-after"] == "30"
    assert app.state.db_pool.demands == [app.state.db_pool.playable_id]
    assert outcome("cold_timeout") == before + 1


async def test_an_object_of_the_wrong_size_is_never_served(app):
    publish(app, CONTENT[:-1])

    response = await request(app, "GET")

    assert response.status_code == 503
    assert app.state.db_pool.demands == [app.state.db_pool.playable_id]


async def test_a_link_at_the_object_name_is_not_followed(app, tmp_path):
    target = tmp_path / "elsewhere.mp4"
    target.write_bytes(CONTENT)
    path = cache_object_path(app.state.playback_cache.root, app.state.db_pool.playable_id)
    path.parent.mkdir(parents=True)
    path.symlink_to(target)

    response = await request(app, "GET")

    assert response.status_code == 503


async def test_an_unreadable_cache_is_503_without_demand(app, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(playback.os, "open", refuse)

    response = await request(app, "GET")

    assert response.status_code == 503 and "retry-after" not in response.headers
    assert app.state.db_pool.demands == []


async def test_a_legacy_head_does_not_start_a_resolution(app):
    app.state.db_pool.managed = False

    response = await request(app, "HEAD")

    assert response.status_code == 405
    app.state.resolver.resolve.assert_not_awaited()


async def test_the_reader_holds_its_lock_until_the_stream_ends_bdd_080(app):
    import fcntl

    path = publish(app)
    handle = app.state.playback_cache.open_reader(app.state.db_pool.playable_id, SIZE)
    other = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        chunks = [chunk async for chunk in playback._stream_handle(handle, start=0, end=9)]
        assert b"".join(chunks) == CONTENT[:10] and handle.closed
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(other)


async def accept(app, expected_sha256: str) -> dict:
    return await stable_acceptance(
        "http://test", str(uuid.uuid4()), SIZE, expected_sha256, transport=ASGITransport(app=app)
    )


async def test_stable_acceptance_passes_against_the_real_endpoint_bdd_068_069(app):
    publish(app)
    digest = hashlib.sha256(CONTENT).hexdigest()

    result = await accept(app, digest)

    assert result == {
        "head": "PASS",
        "get": "PASS",
        "range": "PASS",
        "suffix": "PASS",
        "416": "PASS",
        "sha256": digest,
        "size": SIZE,
    }


async def test_stable_acceptance_rejects_other_bytes_of_the_same_size_bdd_069(app):
    publish(app, bytes(SIZE))

    with pytest.raises(ValueError, match="hash/size"):
        await accept(app, hashlib.sha256(CONTENT).hexdigest())


async def test_stable_acceptance_does_not_pass_a_cold_object(app):
    with pytest.raises(ValueError, match="503"):
        await accept(app, hashlib.sha256(CONTENT).hexdigest())


async def test_a_schema_before_managed_playback_leaves_every_video_legacy():
    pool = AsyncMock()
    pool.fetchrow.side_effect = asyncpg.UndefinedTableError('relation "playable_assets" does not exist')

    assert await playback.resolve_eligibility(pool, uuid.uuid4()) is None
