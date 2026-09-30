"""One synthetic video through real domain services, Redis, FFmpeg and TCP.

External torrent acquisition and Photos are fixtures. This is not live Photos,
Jellyfin, production cleanup, 24-hour quota, or a production Golden Path pass.
"""

import asyncio
import hashlib
import json
import shutil
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import uvicorn
from PIL import Image

from pixav.maxwell_core.media_workflow import MediaWorkflow
from pixav.media_loader.activity import MediaActivityWorker
from pixav.media_loader.preparation import inspect_media
from pixav.media_loader.remuxer import FFmpegRemuxer
from pixav.shared.queue import TaskQueue
from pixav.shared.remote_assets import segment_from_row
from pixav.strm_resolver.app import create_app
from pixav.strm_resolver.playback import PlaybackService
from pixav.strm_resolver.projection import LibraryProjection
from tests.integration.test_media_workflow import seed
from tests.integration.test_storage_workflow import BACKUP, SHARE, add_account, report, submit

pytestmark = pytest.mark.integration


@asynccontextmanager
async def http_server(app):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, ws="none"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if task.done():
                    await task
                    pytest.fail("HTTP server exited before startup")
                if server.started:
                    break
                await asyncio.sleep(0.02)
            assert server.started
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10)


async def create_source(path):
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=128x96:r=25:d=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        str(path),
    )
    assert await asyncio.wait_for(process.wait(), timeout=20) == 0


async def media_stages(workflow, queue, source, root):
    """Real worker/queue; only the torrent result is a known synthetic file."""
    client = AsyncMock()
    client.reconcile_download.return_value = str(source)
    worker = MediaActivityWorker(workflow.pool, client, FFmpegRemuxer(), output_dir=str(root / "prepared"))
    for stage in ("download", "prepare"):
        request = await workflow.next_activity()
        assert request.stage == stage
        await queue.push(request.model_dump(mode="json"))
        assert await worker.run_one(queue)
        assert await workflow.consume_results() == 1
        # Simulated lost ACK/redelivery cannot repeat an external effect.
        await queue.push(request.model_dump(mode="json"))
        assert await worker.run_one(queue)
        assert await workflow.consume_results() == 0
    client.reconcile_download.assert_awaited_once()


class SyntheticPhotos:
    """Bytes retained independently of staging; never contacts Google."""

    def __init__(self, content):
        self.content = content
        self.reads = []

    def reader(self, root):
        async def read_back(segment):
            self.reads.append(root)
            destination = root / str(segment.asset_id) / str(segment.segment_index) / segment.filename
            destination.parent.mkdir(parents=True)
            destination.write_bytes(self.content)
            facts = await inspect_media(str(destination))
            return {
                "method": "photos-original-browser",
                "cold_inputs": "provider-only",
                "size": facts.size_bytes,
                "sha256": facts.sha256,
                "observed": facts.model_dump(mode="json"),
            }

        return SimpleNamespace(read_back=read_back)


async def storage_stages(workflow, photos, root):
    account = await add_account(workflow)
    upload = await workflow.next_activity()
    assert upload.stage == "upload"
    result = report(upload, share_url=SHARE, evidence=BACKUP)
    assert await submit(workflow, result)
    assert await workflow.consume_results() == 1
    assert not await submit(workflow, result)
    assert await workflow.consume_results() == 0
    assert await workflow.pool.fetchval("SELECT state FROM remote_assets") == "CREATED"
    verify = await workflow.next_activity()
    assert verify.stage == "verify"
    segment = segment_from_row(await workflow.pool.fetchrow("SELECT * FROM remote_asset_segments"))
    receipt = await photos.reader(root / "independent-verification").read_back(segment)
    assert await submit(workflow, report(verify, evidence=receipt))
    assert await workflow.consume_results() == 1
    assert await workflow.next_activity() is None
    assert await workflow.pool.fetchval("SELECT state FROM remote_assets") == "DURABLE"
    assert await workflow.pool.fetchval("SELECT state FROM executions") == "SUCCEEDED"
    return account


async def playback_stages(db, video, photos, root, source, prepared):
    tokens = root / "devices.json"
    tokens.write_text(json.dumps({"synthetic": hashlib.sha256(b"synthetic-token").hexdigest()}))
    tokens.chmod(0o600)
    playback = PlaybackService(db, cache_root=root / "cache", readback_factory=photos.reader)
    app = create_app(redis_url=None, db_dsn=None)
    app.state.managed_playback = True
    app.state.playback_settings = SimpleNamespace(playback_tokens_file=str(tokens))
    app.state.db_pool, app.state.playback = db, playback
    art = root / "art"
    art.mkdir()
    poster = art / "poster.png"
    Image.new("RGB", (8, 8), "blue").save(poster)
    async with http_server(app) as base, httpx.AsyncClient(base_url=base, trust_env=False) as client:
        projection = LibraryProjection(db, root=root / "library", artwork_root=art, base_url=base)
        path = f"/stream/{video}"
        assert (await client.get(path)).status_code == 401
        client.headers["Authorization"] = "Bearer synthetic-token"
        response = await client.get(path)
        assert response.status_code == 200 and response.content == photos.content
        active = await projection.publish(video, poster)
        assert (active / "movie.strm").read_text() == base + path
        assert (active / "movie.nfo").is_file() and (active / "poster.png").is_file()
        # Remove only files created by this test; do not invoke production GC or
        # manufacture live-client evidence to make staging cleanup eligible.
        source.unlink()
        prepared.unlink()
        assert await playback.evict(video)
        assert (await client.head(path)).status_code == 200
        assert len(photos.reads) == 2  # Storage verification + initial playback.
        response = await client.get(path, headers={"Range": "bytes=500-799"})
        assert response.status_code == 206 and response.content == photos.content[500:800]
        assert len(photos.reads) == 3 and len(set(photos.reads)) == 3
        response = await client.get(path)
        assert hashlib.sha256(response.content).hexdigest() == hashlib.sha256(photos.content).hexdigest()
        tokens.write_text("{}")
        assert (await client.get(path)).status_code == 403
        await db.execute("UPDATE remote_assets SET state='INVALID',durable_at=NULL")
        assert await projection.withdraw_invalid(video) == 1
        assert not active.exists()
        assert await db.fetchval("SELECT count(*) FROM remote_assets") == 1


async def test_short_synthetic_journey(integration_db, integration_redis, tmp_path, monkeypatch):
    # Dedicated tiny fixture, preserving production disk policy unchanged.
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(total=1000 * 1024**3, free=500 * 1024**3))
    db = integration_db
    for migration in sorted(Path("migrations").glob("*.sql")):
        await db.execute(migration.read_text())
    workflow = MediaWorkflow(db, backoff=(1, 2), lease_seconds=120)
    video, _ = await seed(workflow)
    source = tmp_path / "synthetic.mkv"
    await create_source(source)
    await media_stages(workflow, TaskQueue(*integration_redis), source, tmp_path)
    prepared = Path(await db.fetchval("SELECT local_path FROM videos WHERE id=$1", video))
    photos = SyntheticPhotos(prepared.read_bytes())
    account = await storage_stages(workflow, photos, tmp_path)
    await playback_stages(db, video, photos, tmp_path, source, prepared)
    assert await db.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == len(photos.content)
    assert await db.fetchval("SELECT count(*) FROM activity_attempts WHERE stage='upload'") == 1
    assert not source.exists() and not prepared.exists()
