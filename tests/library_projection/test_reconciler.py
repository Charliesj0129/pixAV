"""Reconciler decisions with an in-memory repository and a real library directory.

The PostgreSQL behaviour is covered by tests/integration/test_library_projection.py;
these cover the paths a database cannot easily produce.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from pixav.library_projection.artwork import ArtworkStore
from pixav.library_projection.reconciler import LibraryReconciler
from pixav.library_projection.repository import ArtworkRecord, ProjectionSource, Publication
from pixav.library_projection.writer import ProjectionWriter


class MemoryRepository:
    def __init__(self, sources: list[ProjectionSource], writer: ProjectionWriter) -> None:
        self.sources = sources
        poster = writer.artwork.root.parent / "poster.png"
        poster.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        digest, size, mime = writer.artwork.import_file(poster)
        self.artwork = {
            s.video_id: [
                ArtworkRecord(
                    id=uuid.uuid4(), video_id=s.video_id, kind="poster", sha256=digest, size_bytes=size, media_type=mime
                )
            ]
            for s in sources
        }
        self.rows: dict[uuid.UUID, Publication] = {}
        self.failures: list[tuple[uuid.UUID, str]] = []

    @asynccontextmanager
    async def exclusive(self):
        yield True

    async def eligible_sources(self):
        return list(self.sources)

    async def publications(self):
        return dict(self.rows)

    async def overrides_for(self, ids):
        return {}

    async def artwork_for(self, ids):
        return self.artwork

    async def begin_attempt(self, video_id, *, desired_digest, layout_version):
        return None

    async def mark_published(self, video_id, *, playable_asset_id, digest, version, projection_version, **_):
        self.rows[video_id] = Publication(
            video_id=video_id,
            playable_asset_id=playable_asset_id,
            state="PUBLISHED",
            projection_version=projection_version,
            desired_digest=digest,
            active_digest=digest,
            active_version=version,
        )
        return True

    async def record_failure(self, video_id, code, *, layout_version):
        self.failures.append((video_id, code))

    async def mark_withdrawn(self, video_id, reason):
        self.rows[video_id] = Publication(video_id=video_id, state="PENDING")

    async def publication_counts(self):
        return {}


def source(title: str = "Synthetic") -> ProjectionSource:
    return ProjectionSource(video_id=uuid.uuid4(), playable_asset_id=uuid.uuid4(), title=title)


@pytest.fixture
def writer(tmp_path) -> ProjectionWriter:
    return ProjectionWriter(tmp_path / "root", ArtworkStore(tmp_path / "artwork"))


async def test_writes_per_pass_are_bounded(writer):
    repository = MemoryRepository([source(), source(), source()], writer)
    reconciler = LibraryReconciler(repository, writer, resolver_base_url="http://r:8000", write_limit=2)

    assert await reconciler.reconcile() == {"published": 2, "deferred": 1}
    assert await reconciler.reconcile() == {"unchanged": 2, "published": 1}


async def test_a_base_url_that_could_leak_fails_every_item(writer):
    repository = MemoryRepository([source()], writer)
    reconciler = LibraryReconciler(repository, writer, resolver_base_url="http://user:pw@r:8000")

    assert await reconciler.reconcile() == {"failed": 1}
    assert repository.failures[0][1] == "RESOLVER_BASE_URL_INVALID"
    assert not writer.library.exists()


async def test_foreign_content_at_the_item_name_is_reported_not_replaced(writer):
    item = source()
    writer.library.mkdir(parents=True)
    (writer.library / f"pixav-{item.video_id}").write_text("someone else's file")
    repository = MemoryRepository([item], writer)

    assert await LibraryReconciler(repository, writer, resolver_base_url="http://r:8000").reconcile() == {"failed": 1}
    assert repository.failures == [(item.video_id, "PROJECTION_PATH_CONFLICT")]
    assert (writer.library / f"pixav-{item.video_id}").read_text() == "someone else's file"


async def test_a_new_playable_object_with_the_same_projection_is_recorded_not_rewritten(writer):
    item = source()
    repository = MemoryRepository([item], writer)
    reconciler = LibraryReconciler(repository, writer, resolver_base_url="http://r:8000")
    await reconciler.reconcile()
    version = repository.rows[item.video_id].active_version
    repository.sources = [item.model_copy(update={"playable_asset_id": uuid.uuid4()})]

    assert await reconciler.reconcile() == {"recorded": 1}
    assert repository.rows[item.video_id].active_version == version


async def test_missing_poster_refuses_publication(writer):
    item = source()
    repository = MemoryRepository([item], writer)
    repository.artwork = {}
    reconciler = LibraryReconciler(repository, writer, resolver_base_url="http://r:8000")
    assert await reconciler.reconcile() == {"failed": 1}
    assert repository.failures == [(item.video_id, "POSTER_REQUIRED")]
    assert not writer.library.exists()
