"""Library projection against real PostgreSQL and a real filesystem (BDD-085-108).

READY objects are committed through the same authority path the playback
worker reports into; the library root is a temporary directory. Nothing here
shows that Jellyfin scanned or played an item.
"""

import json
import os
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from pixav.library_projection.artwork import ArtworkStore
from pixav.library_projection.reconciler import LibraryReconciler
from pixav.library_projection.repository import LibraryRepository
from pixav.library_projection.writer import ProjectionWriter
from pixav.maxwell_core.gc import LocalFileJanitor
from pixav.maxwell_core.media_workflow import MediaWorkflow
from tests.integration.test_playback_workflow import durable_video
from tests.integration.test_playback_workflow import ready as ready_without_artwork

pytestmark = pytest.mark.integration

BASE = "http://resolver.lan:8000"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic poster"


async def ready(workflow, store):
    video, asset = await ready_without_artwork(workflow)
    store.root.mkdir(parents=True, exist_ok=True)
    source = store.root / "fixture.png"
    source.write_bytes(PNG)
    digest, size, media_type = store.import_file(source)
    await LibraryRepository(workflow.pool).record_artwork(
        video, "poster", sha256=digest, size_bytes=size, media_type=media_type, provenance={"source": "fixture"}
    )
    return video, asset


@pytest.fixture
async def workflow(integration_db):
    for path in sorted(Path("migrations").glob("*.sql")):
        await integration_db.execute(path.read_text())
    return MediaWorkflow(integration_db, backoff=(1, 2), lease_seconds=120)


@pytest.fixture
def store(tmp_path) -> ArtworkStore:
    return ArtworkStore(tmp_path / "artwork")


@pytest.fixture
def library(workflow, tmp_path, store) -> LibraryReconciler:
    return LibraryReconciler(
        LibraryRepository(workflow.pool), ProjectionWriter(tmp_path / "root", store), resolver_base_url=BASE
    )


def item(library, video) -> Path:
    return library.writer.library / f"pixav-{video}"


async def publication(workflow, video):
    return await workflow.pool.fetchrow("SELECT * FROM library_publications WHERE video_id=$1", video)


async def test_only_a_ready_object_on_a_durable_copy_is_published_bdd_085_086_087(workflow, library, store):
    video, _ = await ready(workflow, store)
    durable_only, _ = await durable_video(workflow.pool)

    counts = await library.reconcile()

    assert counts == {"published": 1}
    assert (item(library, video) / f"pixav-{video}.strm").read_text() == f"{BASE}/stream/{video}\n"
    assert not os.path.lexists(item(library, durable_only)) and await publication(workflow, durable_only) is None
    row = await publication(workflow, video)
    playable = await workflow.pool.fetchval("SELECT id FROM playable_assets WHERE video_id=$1", video)
    assert row["state"] == "PUBLISHED" and row["playable_asset_id"] == playable
    assert json.loads(row["effective_metadata"])["title"] == "synthetic fixture"


async def test_a_second_pass_changes_nothing_bdd_089(workflow, library, store):
    video, _ = await ready(workflow, store)
    await library.reconcile()
    before = (await publication(workflow, video))["active_version"]

    assert await library.reconcile() == {"unchanged": 1}
    assert (await publication(workflow, video))["active_version"] == before
    assert os.listdir(library.writer.library) == [f"pixav-{video}"]


async def test_a_deleted_library_is_rebuilt_from_postgresql_bdd_103_104_106(workflow, library, tmp_path, store):
    video, _ = await ready(workflow, store)
    await library.reconcile()
    shutil.rmtree(tmp_path / "root")

    assert await library.reconcile() == {"published": 1}
    assert (item(library, video) / f"pixav-{video}.nfo").exists()
    assert (await publication(workflow, video))["projection_version"] == 2


async def test_override_refreshes_the_item_and_survives_a_scrape_bdd_095_096_105(workflow, library, store):
    video, _ = await ready(workflow, store)
    await library.reconcile()
    repository = LibraryRepository(workflow.pool)
    await repository.set_override(video, "title", "Manual Title", operator="fixture-operator", reason="fixture")

    assert await library.reconcile() == {"published": 1}
    await workflow.pool.execute(
        """UPDATE videos SET metadata_json='{"stash": {"found": true, "title": "Scraped", "studio": "S"}}'
        WHERE id=$1""",
        video,
    )
    assert await library.reconcile() == {"published": 1}, "the new studio is published"

    nfo = (item(library, video) / f"pixav-{video}.nfo").read_text()
    assert "<title>Manual Title</title>" in nfo and "<studio>S</studio>" in nfo and "Scraped" not in nfo
    effective = json.loads((await publication(workflow, video))["effective_metadata"])
    assert effective["provenance"] == {"title": ["manual"], "studio": ["stash"]}


async def test_retained_poster_is_published_and_outlives_its_source_bdd_098_100(workflow, library, store, tmp_path):
    video, _ = await ready(workflow, store)
    source = tmp_path / "poster.png"
    source.write_bytes(PNG)
    digest, size, media_type = store.import_file(source)
    await LibraryRepository(workflow.pool).record_artwork(
        video, "poster", sha256=digest, size_bytes=size, media_type=media_type, provenance={"source": "fixture"}
    )
    source.unlink()

    await library.reconcile()
    shutil.rmtree(tmp_path / "root")
    await library.reconcile()

    assert (item(library, video) / "poster.png").read_bytes() == PNG
    assert not (item(library, video) / "backdrop.png").exists()


async def test_damaged_artwork_fails_and_keeps_the_active_item_bdd_101(workflow, library, store, tmp_path):
    video, _ = await ready(workflow, store)
    await library.reconcile()
    active = (await publication(workflow, video))["active_version"]
    source = tmp_path / "poster.png"
    source.write_bytes(PNG + b"updated poster")
    digest, size, media_type = store.import_file(source)
    await LibraryRepository(workflow.pool).record_artwork(
        video, "poster", sha256=digest, size_bytes=size, media_type=media_type, provenance={"source": "fixture"}
    )
    store.path_for(digest).write_bytes(b"damaged")

    assert await library.reconcile() == {"failed": 1}

    row = await publication(workflow, video)
    assert row["state"] == "FAILED" and row["failure_code"] == "ARTWORK_UNAVAILABLE"
    assert row["active_version"] == active and library.writer.active_version(video) == active


async def test_files_swapped_before_a_crash_are_recorded_not_rewritten(workflow, library, store):
    video, _ = await ready(workflow, store)
    [decision] = await library.plan()
    version = library.writer.publish(decision.prepared.spec, 7)

    assert await library.reconcile() == {"recorded": 1}

    row = await publication(workflow, video)
    assert row["state"] == "PUBLISHED" and row["active_version"] == version and row["projection_version"] == 7


async def test_an_object_that_stops_playing_is_withdrawn_and_nothing_remote_changes_bdd_107_108(
    workflow, library, store
):
    video, asset = await ready(workflow, store)
    await library.reconcile()
    remote_before = await workflow.pool.fetchrow("SELECT * FROM remote_assets WHERE id=$1", asset)
    segments_before = await workflow.pool.fetch("SELECT * FROM remote_asset_segments WHERE asset_id=$1", asset)
    await workflow.pool.execute("UPDATE playable_assets SET state='STALE', updated_at=now() WHERE video_id=$1", video)

    assert await library.reconcile() == {"withdrawn": 1}

    assert not os.path.lexists(item(library, video))
    row = await publication(workflow, video)
    assert row["state"] == "PENDING" and row["withdrawn_reason"] == "NOT_PLAYABLE" and row["active_version"] is None
    assert await workflow.pool.fetchrow("SELECT * FROM remote_assets WHERE id=$1", asset) == remote_before
    assert await workflow.pool.fetch("SELECT * FROM remote_asset_segments WHERE asset_id=$1", asset) == segments_before


async def test_items_left_on_disk_for_unknown_videos_are_withdrawn(workflow, library, store):
    stray = uuid.uuid4()
    (library.writer.versions / str(stray) / "1-aaaaaaaaaaaa").mkdir(parents=True)

    assert await library.reconcile() == {"withdrawn": 1}
    assert not (library.writer.versions / str(stray)).exists()


async def test_publication_completes_the_cleanup_condition_bdd_005_058(workflow, library, tmp_path, store):
    video, _ = await ready(workflow, store)
    janitor = LocalFileJanitor(workflow.pool, download_dir=str(tmp_path), library_root=str(tmp_path / "root"))

    async with workflow.pool.acquire() as conn:
        before = await janitor._evaluate(conn, video, "/staging/x")
    await library.reconcile()
    async with workflow.pool.acquire() as conn:
        after = await janitor._evaluate(conn, video, "/staging/x")

    assert before["complete_publication"] is False and after["complete_publication"] is True


async def test_only_one_pass_runs_at_a_time(workflow, library, store):
    await ready(workflow, store)
    async with LibraryRepository(workflow.pool).exclusive() as held:
        assert held
        assert await library.reconcile() == {"skipped": 1}
    assert await library.reconcile() == {"published": 1}


async def test_override_values_are_typed_by_the_database(workflow, store):
    video, _ = await ready(workflow, store)
    with pytest.raises(asyncpg.CheckViolationError):
        await workflow.pool.execute(
            """INSERT INTO video_metadata_overrides(video_id,field,value,operator,reason)
            VALUES($1,'tags','"not-a-list"','fixture-operator','fixture')""",
            video,
        )
    repository = LibraryRepository(workflow.pool)
    await repository.set_override(video, "tags", ["b", "a"], operator="fixture-operator", reason="fixture")
    assert await repository.overrides(video) == {"tags": ["b", "a"]}
    assert await repository.clear_override(video, "tags", operator="fixture-operator", reason="fixture")
    assert await repository.overrides(video) == {}


async def test_the_authority_role_can_reconcile(workflow, tmp_path, store):
    """The reconciler runs inside Maxwell's tick, under the execution authority's grants."""
    video, _ = await ready(workflow, store)

    async def set_role(conn):
        await conn.execute("SET ROLE pixav_execution_authority")

    database = await workflow.pool.fetchval("SELECT current_database()")
    endpoint = urlsplit(
        os.getenv("PIXAV_E2E_ADMIN_DSN", "postgresql://pixav_test:integration-only@127.0.0.1:15432/pixav_integration")
    )
    pool = await asyncpg.create_pool(
        dsn=urlunsplit(endpoint._replace(path=f"/{database}")), setup=set_role, min_size=1, max_size=2
    )
    try:
        authority = LibraryReconciler(
            LibraryRepository(pool), ProjectionWriter(tmp_path / "root", store), resolver_base_url=BASE
        )
        assert await authority.reconcile() == {"published": 1}
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool.execute(
                """INSERT INTO video_metadata_overrides(video_id,field,value,operator,reason)
                VALUES($1,'title','"x"','fixture-operator','fixture')""",
                video,
            )
    finally:
        await pool.close()


async def test_missing_poster_never_becomes_published(workflow, library):
    video, _ = await ready_without_artwork(workflow)
    assert await library.reconcile() == {"failed": 1}
    row = await publication(workflow, video)
    assert row["state"] == "FAILED" and row["failure_code"] == "POSTER_REQUIRED"
    assert not os.path.lexists(item(library, video))
