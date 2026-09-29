"""Real PostgreSQL gates/recovery; synthetic bytes replace only the Photos boundary.

These are isolation contracts, never live Photos or Jellyfin acceptance.
"""

# ruff: noqa: S607 -- fixed FFmpeg command for disposable synthetic media

import asyncio
import json
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from fastapi import HTTPException
from PIL import Image

from pixav.media_loader.preparation import inspect_media
from pixav.strm_resolver.playback import PlaybackService
from pixav.strm_resolver.projection import LibraryProjection
from tests.integration.test_cleanup_gate import artifact

pytestmark = pytest.mark.integration


@pytest.fixture
async def ready_fixture(integration_db, tmp_path, monkeypatch):
    # The fixture is a tiny synthetic clip; production keeps the 100 GiB latch.
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(total=1000 * 1024**3, free=500 * 1024**3))
    db = integration_db
    for path in sorted(Path("migrations").glob("*.sql")):
        await db.execute(path.read_text())
    video, staging = await artifact(db, tmp_path)
    subprocess.run(  # noqa: S603,S607 -- synthetic media in a dedicated test directory
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=128x96:r=25:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(staging),
        ],
        check=True,
        timeout=30,
    )
    facts = await inspect_media(str(staging))
    provider_bytes = staging.read_bytes()
    asset_id = await db.fetchval("SELECT id FROM remote_assets WHERE video_id=$1", video)
    await db.execute(
        "UPDATE remote_assets SET policy_version='photos-original-v2',expected=$2::jsonb WHERE id=$1",
        asset_id,
        facts.model_dump_json(),
    )
    await db.execute(
        """INSERT INTO remote_asset_segments(asset_id,segment_index,start_seconds,end_seconds,size_bytes,sha256,
        local_path,media_info,share_url,usage_counted_at,state)
        VALUES($1,0,0,$2,$3,$4,$5,'{}','https://photos.google.com/synthetic',now(),'verified')""",
        asset_id,
        facts.duration_seconds,
        facts.size_bytes,
        facts.sha256,
        str(staging),
    )
    calls = []

    def retriever(root):
        async def read_back(segment):
            calls.append(root)
            destination = root / str(segment.asset_id) / str(segment.segment_index) / segment.filename
            destination.parent.mkdir(parents=True)
            destination.write_bytes(provider_bytes)
            return {
                "method": "photos-original-browser",
                "cold_inputs": "provider-only",
                "size": facts.size_bytes,
                "sha256": facts.sha256,
                "observed": facts.model_dump(mode="json"),
            }

        return SimpleNamespace(read_back=read_back)

    playback = PlaybackService(db, cache_root=tmp_path / "cache", readback_factory=retriever)
    projection = LibraryProjection(
        db, root=tmp_path / "library", artwork_root=tmp_path / "artwork", base_url="https://playback.example.test"
    )
    projection.artwork_root.mkdir()
    poster = projection.artwork_root / "poster.png"
    Image.new("RGB", (8, 8), "blue").save(poster)
    return SimpleNamespace(
        db=db,
        video=video,
        asset=asset_id,
        staging=staging,
        playback=playback,
        projection=projection,
        poster=poster,
        facts=facts,
        calls=calls,
    )


async def test_cold_rebuild_after_staging_and_cache_loss(ready_fixture):
    f = ready_fixture
    await f.playback.prepare(f.video)
    first = await f.db.fetchrow("SELECT * FROM playable_assets")
    assert first["state"] == "READY" and first["playback_verified_at"] is None
    assert first["sha256"] == f.facts.sha256
    await f.playback.prepare(f.video)
    assert len(f.calls) == 1
    f.staging.unlink()  # exact disposable synthetic fixture, not operational cleanup
    assert await f.playback.evict(f.video)
    await f.playback.prepare(f.video)
    assert len(f.calls) == 2 and f.calls[0] != f.calls[1]
    assert await f.db.fetchval("SELECT state FROM remote_assets") == "DURABLE"
    assert await f.db.fetchval("SELECT count(*) FROM remote_asset_segments") == 1


async def test_created_remote_never_becomes_ready(ready_fixture):
    f = ready_fixture
    await f.db.execute("UPDATE remote_assets SET state='CREATED',durable_at=NULL")
    with pytest.raises(HTTPException) as error:
        await f.playback.prepare(f.video)
    assert error.value.status_code == 409 and not f.calls
    assert await f.db.fetchval("SELECT count(*) FROM playable_assets") == 0


async def test_reader_prevents_eviction_until_response_finishes(ready_fixture):
    f = ready_fixture
    await f.playback.prepare(f.video)
    async with f.playback.reader(f.video) as item:
        # Repeated seek preparation must remain concurrent with an active stream.
        await asyncio.wait_for(f.playback.prepare(f.video), timeout=3)
        eviction = asyncio.create_task(f.playback.evict(f.video))
        done, _ = await asyncio.wait([eviction], timeout=0.1)
        assert not done
        assert Path(item["cache_path"]).is_file()
    assert await asyncio.wait_for(eviction, timeout=3)


async def test_library_rebuild_and_invalidation(ready_fixture):
    f = ready_fixture
    with pytest.raises(ValueError, match="ready"):
        await f.projection.publish(f.video, f.poster)
    await f.playback.prepare(f.video)
    await f.db.execute(
        "UPDATE videos SET metadata_json=$2::jsonb WHERE id=$1",
        f.video,
        json.dumps({"manual_overrides": {"title": "Synthetic override", "tags": ["fixture"]}}),
    )
    active = await f.projection.publish(f.video, f.poster)
    assert (active / "movie.strm").read_text() == f"https://playback.example.test/stream/{f.video}"
    assert "Synthetic override" in (active / "movie.nfo").read_text()
    original = active.readlink()
    assert await f.projection.publish(f.video) == active and active.readlink() == original
    shutil.rmtree(f.projection.root)
    await f.projection.publish(f.video)
    assert (active / "poster.png").read_bytes() == f.poster.read_bytes()
    await f.db.execute("UPDATE remote_assets SET state='INVALID',durable_at=NULL")
    assert await f.db.fetchval("SELECT state FROM playable_assets") == "INVALID"
    assert await f.projection.withdraw_invalid() == 1
    assert not active.exists() and f.poster.exists()
    assert await f.db.fetchval("SELECT count(*) FROM remote_assets") == 1


async def test_commit_failure_restores_previous_projection(ready_fixture):
    f = ready_fixture
    await f.playback.prepare(f.video)
    active = await f.projection.publish(f.video, f.poster)
    old_target = active.readlink()
    await f.db.execute("UPDATE videos SET title='new synthetic title'")
    await f.db.execute("""CREATE FUNCTION refuse_publication() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'synthetic commit failure'; END $$;
        CREATE CONSTRAINT TRIGGER refuse_publication AFTER UPDATE ON library_publications
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION refuse_publication();""")
    with pytest.raises(Exception, match="synthetic commit failure"):
        await f.projection.publish(f.video)
    assert active.readlink() == old_target


async def test_corrupt_readback_never_becomes_ready(ready_fixture):
    f = ready_fixture

    def bad_retriever(root):
        async def read_back(segment):
            return {"method": "photos-original-browser", "cold_inputs": "provider-only", "size": 1, "sha256": "a" * 64}

        return SimpleNamespace(read_back=read_back)

    f.playback.readback_factory = bad_retriever
    with pytest.raises(ValueError):
        await f.playback.prepare(f.video)
    assert await f.db.fetchval("SELECT count(*) FROM playable_assets") == 0
    assert f.staging.exists()


async def test_low_space_stops_before_remote_retrieval(ready_fixture, monkeypatch):
    from pixav.media_loader.video_parts import MediaOperationError

    f = ready_fixture
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(total=1000 * 1024**3, free=99 * 1024**3))
    with pytest.raises(MediaOperationError, match="disk latch"):
        await f.playback.prepare(f.video)
    assert not f.calls
    assert await f.db.fetchval("SELECT count(*) FROM playable_assets") == 0
    assert f.staging.exists()


async def test_playback_role_cannot_change_remote_durability(ready_fixture):
    f = ready_fixture
    async with f.db.acquire() as conn:

        class SingleConnection:
            @asynccontextmanager
            async def acquire(self):
                yield conn

        await conn.execute("SET ROLE pixav_playback")
        try:
            f.playback.pool = SingleConnection()
            await f.playback.prepare(f.video)
            async with f.playback.reader(f.video) as ready:
                assert ready["state"] == "READY"
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("UPDATE remote_assets SET state='INVALID',durable_at=NULL")
        finally:
            await conn.execute("RESET ROLE")


async def test_http_playback_recovers_after_synthetic_staging_and_cache_removal(ready_fixture, tmp_path):
    """Real DB, FFmpeg media and HTTP transport; only Photos is synthetic."""
    import hashlib

    from httpx import ASGITransport, AsyncClient

    from pixav.strm_resolver.app import create_app

    f = ready_fixture
    original = f.staging.read_bytes()
    f.staging.unlink()  # Disposable test media, not the production cleanup operation.
    tokens = tmp_path / "devices.json"
    tokens.write_text(json.dumps({"fixture": hashlib.sha256(b"synthetic-token").hexdigest()}))
    tokens.chmod(0o600)
    app = create_app(redis_url=None, db_dsn=None)
    app.state.managed_playback = True
    app.state.playback_settings = SimpleNamespace(playback_tokens_file=str(tokens))
    app.state.db_pool = f.db
    app.state.playback = f.playback
    url = f"/stream/{f.video}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture") as client:
        assert (await client.get(url)).status_code == 401
        assert f.calls == []
        client.headers["Authorization"] = "Bearer synthetic-token"
        first = await client.get(url)
        assert first.status_code == 200
        assert hashlib.sha256(first.content).hexdigest() == f.facts.sha256
        active = await f.projection.publish(f.video, f.poster)
        assert active.is_dir()
        assert await f.playback.evict(f.video)
        head = await client.head(url)
        assert head.status_code == 200
        assert int(head.headers["content-length"]) == len(original)
        assert len(f.calls) == 1  # HEAD does not restore bytes.
        recovered = await client.get(url, headers={"Range": "bytes=500-799"})
        assert recovered.status_code == 206 and recovered.content == original[500:800]
        assert "location" not in recovered.headers
        assert len(f.calls) == 2
        assert f.calls[0] != f.calls[1]
        tokens.write_text("{}")
        assert (await client.get(url)).status_code == 403
        assert len(f.calls) == 2
    assert not f.staging.exists()
    assert await f.db.fetchval("SELECT state FROM remote_assets WHERE id=$1", f.asset) == "DURABLE"
