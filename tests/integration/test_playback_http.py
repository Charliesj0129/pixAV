"""The stable stream URL over real HTTP, backed by real PostgreSQL facts.

The resolver runs under uvicorn with a pool restricted to
``pixav_playback_reader``, so every query it makes -- eligibility, demand, and
the legacy path -- is proven to need nothing that role lacks. The cache object
is written by the test: what this shows is that pixAV serves exactly the READY
bytes and asks for a rebuild when they are gone, not that Photos returned them.
"""

import asyncio
import contextlib
import os
import socket
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
import uvicorn

from pixav.maxwell_core.media_workflow import MediaWorkflow
from pixav.shared.playable_assets import PlayableAssetRepository
from pixav.shared.playback_models import cache_object_path
from pixav.strm_resolver.app import create_app
from pixav.strm_resolver.movie_acceptance import stable_acceptance
from pixav.strm_resolver.playback import PlaybackCache
from tests.integration.test_playback_workflow import CONTENT, DIGEST, durable_video, playable, ready, receipt, report

pytestmark = pytest.mark.integration


@pytest.fixture
async def workflow(integration_db):
    for path in sorted(Path("migrations").glob("*.sql")):
        await integration_db.execute(path.read_text())
    return MediaWorkflow(integration_db, backoff=(1, 2), lease_seconds=120)


@pytest.fixture
async def reader_pool(workflow):
    async def set_role(conn):
        await conn.execute("SET ROLE pixav_playback_reader")

    database = await workflow.pool.fetchval("SELECT current_database()")
    endpoint = urlsplit(
        os.getenv("PIXAV_E2E_ADMIN_DSN", "postgresql://pixav_test:integration-only@127.0.0.1:15432/pixav_integration")
    )
    pool = await asyncpg.create_pool(
        dsn=urlunsplit(endpoint._replace(path=f"/{database}")), setup=set_role, min_size=1, max_size=2
    )
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def resolver(reader_pool, tmp_path):
    """A real HTTP resolver on an ephemeral loopback port."""
    app = create_app(redis_url=None, db_dsn=None)
    app.state.db_pool = reader_pool
    app.state.playback_cache = PlaybackCache(tmp_path / "cache")
    app.state.playback_cold_wait_seconds = 0
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", ws="none", log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield app, f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await serving
        listener.close()


def publish(app, playable_id, content: bytes = CONTENT) -> None:
    path = cache_object_path(app.state.playback_cache.root, playable_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


async def test_ready_object_passes_stable_acceptance_and_records_evidence_bdd_064_068_069(workflow, resolver):
    app, base = resolver
    video, _ = await ready(workflow)
    row = (await playable(workflow, video))[0]
    publish(app, row["id"])

    result = await stable_acceptance(base, str(video), row["size_bytes"], row["sha256"])

    assert result["sha256"] == DIGEST and result["get"] == "PASS" and result["416"] == "PASS"
    assert await PlayableAssetRepository(workflow.pool).record_playback_verification(row["id"], result)
    assert (await playable(workflow, video))[0]["playback_verified_at"] is not None


async def test_cold_request_records_demand_that_opens_a_rebuild_bdd_078_081(workflow, resolver):
    app, base = resolver
    video, _ = await ready(workflow)
    row = (await playable(workflow, video))[0]

    async with httpx.AsyncClient() as client:
        cold = await client.get(f"{base}/stream/{video}", headers={"Range": "bytes=0-3"})
    assert cold.status_code == 503 and cold.headers["retry-after"] == "30"
    assert await workflow.pool.fetchval(
        "SELECT request_count FROM playback_cache_requests WHERE playable_asset_id=$1", row["id"]
    )

    await workflow.playback.open_due()
    request = await workflow.next_activity()
    assert request.stage == "playback" and request.purpose == "rebuild" and request.playable_asset_id == row["id"]
    await report(workflow, request, evidence=receipt(inputs="photos"))
    publish(app, row["id"])

    async with httpx.AsyncClient() as client:
        warm = await client.get(f"{base}/stream/{video}", headers={"Range": "bytes=0-3"})
    assert warm.status_code == 206 and warm.content == CONTENT[:4]
    assert await workflow.playback.open_due() == [], "a served object needs no further rebuild"


async def test_copy_that_is_not_durable_never_plays_bdd_067_082(workflow, resolver):
    app, base = resolver
    video, asset = await durable_video(workflow.pool)
    await workflow.playback.open_due()
    preparing = (await playable(workflow, video))[0]
    publish(app, preparing["id"])  # bytes on disk do not make a PREPARING object playable

    async with httpx.AsyncClient() as client:
        before = await client.get(f"{base}/stream/{video}")
        await workflow.pool.execute("UPDATE remote_assets SET state='VERIFIED', durable_at=NULL WHERE id=$1", asset)
        after = await client.head(f"{base}/stream/{video}")

    assert before.status_code == 409 and CONTENT not in before.content
    assert after.status_code == 409


async def test_legacy_video_keeps_its_path_under_the_reader_role(workflow, resolver):
    _, base = resolver
    video = await workflow.pool.fetchval(
        "INSERT INTO videos(title, share_url) VALUES('synthetic legacy', $1) RETURNING id",
        f"pixav-local://{uuid.uuid4()}",
    )

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{base}/stream/{video}")

    assert response.status_code == 302 and response.headers["location"].endswith(f"/local/{video}")
    assert await workflow.pool.fetchval("SELECT status FROM videos WHERE id=$1", video) == "available"


async def test_integrity_of_the_served_bytes_is_the_ready_digest(workflow, resolver):
    """Same size, other bytes: acceptance refuses and nothing is recorded."""
    app, base = resolver
    video, _ = await ready(workflow)
    row = (await playable(workflow, video))[0]
    publish(app, row["id"], bytes(len(CONTENT)))

    with pytest.raises(ValueError, match="hash/size"):
        await stable_acceptance(base, str(video), row["size_bytes"], row["sha256"])

    assert (await playable(workflow, video))[0]["playback_verified_at"] is None
