"""Tests for OrphanTaskCleaner."""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from pixav.maxwell_core.gc import (
    LocalFileJanitor,
    OrphanTaskCleaner,
    _parse_update_count,
    safe_cleanup_candidate,
)


@pytest.fixture
def mock_pool() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def cleaner(mock_pool: AsyncMock) -> OrphanTaskCleaner:
    return OrphanTaskCleaner(mock_pool, max_age=timedelta(hours=1))


class TestOrphanTaskCleaner:
    async def test_cleanup_finds_orphans(self, cleaner: OrphanTaskCleaner, mock_pool: AsyncMock) -> None:
        mock_pool.execute.return_value = "UPDATE 3"

        count = await cleaner.cleanup()

        assert count == 3
        mock_pool.execute.assert_awaited_once()
        query = mock_pool.execute.call_args[0][0]
        assert "state = $1" in query
        assert "updated_at < now()" in query

    async def test_cleanup_no_orphans(self, cleaner: OrphanTaskCleaner, mock_pool: AsyncMock) -> None:
        mock_pool.execute.return_value = "UPDATE 0"

        count = await cleaner.cleanup()
        assert count == 0

    async def test_cleanup_expired_videos(self, cleaner: OrphanTaskCleaner, mock_pool: AsyncMock) -> None:
        mock_pool.execute.return_value = "UPDATE 5"

        count = await cleaner.cleanup_expired_videos()
        assert count == 5


class TestParseUpdateCount:
    def test_parses_update_n(self) -> None:
        assert _parse_update_count("UPDATE 42") == 42

    def test_parses_zero(self) -> None:
        assert _parse_update_count("UPDATE 0") == 0

    def test_invalid_returns_zero(self) -> None:
        assert _parse_update_count("") == 0
        assert _parse_update_count("INSERT 1") == 1

    def test_unparseable_returns_zero(self) -> None:
        assert _parse_update_count("ERROR") == 0


def test_safe_cleanup_accepts_regular_file_inside_root(tmp_path) -> None:
    media = tmp_path / "video.mp4"
    media.write_bytes(b"video")
    target, missing = safe_cleanup_candidate(str(tmp_path), str(media))
    assert target == media
    assert missing is False


def test_safe_cleanup_rejects_outside_and_symlink(tmp_path) -> None:
    outside = tmp_path.parent / "outside.mp4"
    target, _ = safe_cleanup_candidate(str(tmp_path), str(outside))
    assert target is None

    real = tmp_path / "real.mp4"
    real.write_bytes(b"video")
    link = tmp_path / "link.mp4"
    link.symlink_to(real)
    target, _ = safe_cleanup_candidate(str(tmp_path), str(link))
    assert target is None


def test_safe_cleanup_rejects_absolute_parent_traversal(tmp_path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"keep")
    disguised = root / "nested" / ".." / ".." / outside.name

    target, missing = safe_cleanup_candidate(str(root), str(disguised))

    assert target is None
    assert missing is False
    assert outside.read_bytes() == b"keep"


class _FakeTransaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _FakeConnection:
    """A connection that answers the janitor's gate queries deterministically."""

    def __init__(self, *, durable: bool, open_work: bool, reader: bool, due: bool, publications: bool) -> None:
        self._answers = {"durable": durable, "open_work": open_work, "reader": reader, "due": due}
        self._publications = publications
        self.audits: list[tuple] = []
        self.updates: list[str] = []

    async def fetchval(self, sql: str, *args):
        if "to_regclass" in sql:
            # Only the projection table these fakes can stand in for exists.
            # ``playable_assets`` and ``cleanup_authorizations`` belong to
            # Component D, which has not landed, so they answer absent.
            return args[0] if (self._publications and args[0] == "public.library_publications") else None
        if "library_publications" in sql:
            return True
        return None

    async def fetchrow(self, sql: str, *args):
        if "cleanup_authorizations" in sql:
            return None
        return dict(self._answers)

    async def execute(self, sql: str, *args):
        if "cleanup_audit" in sql:
            self.audits.append(args)
        elif not sql.lstrip().upper().startswith("SELECT"):
            # A row lock is not a change; only real writes count as mutations.
            self.updates.append(sql)
        return "UPDATE 1"

    def transaction(self):
        return _FakeTransaction()


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, rows, conn):
        self._rows = rows
        self.conn = conn

    async def fetch(self, sql: str, *args):
        return self._rows

    def acquire(self):
        return _FakeAcquire(self.conn)


def _janitor_pool(media, *, durable, open_work=False, reader=False, due=True, publications=False):
    conn = _FakeConnection(durable=durable, open_work=open_work, reader=reader, due=due, publications=publications)
    rows = [{"id": uuid.uuid4(), "local_path": str(media)}]
    return _FakePool(rows, conn), conn


class TestLocalFileJanitor:
    async def test_no_durable_remote_asset_preserves_artifact_bdd_004_130(self, tmp_path) -> None:
        media = tmp_path / "synthetic.mp4"
        media.write_bytes(b"recoverable")
        pool, conn = _janitor_pool(media, durable=False)

        stats = await LocalFileJanitor(pool, download_dir=str(tmp_path), apply_deletions=True).cleanup()

        assert stats["deleted"] == 0 and stats["rejected"] == 1
        assert media.read_bytes() == b"recoverable"
        assert conn.audits and conn.audits[0][3] == "rejected"

    async def test_durable_without_publication_is_still_rejected_bdd_005_131(self, tmp_path) -> None:
        """Component D/E are absent, so no artifact can show a complete projection."""
        media = tmp_path / "synthetic.mp4"
        media.write_bytes(b"recoverable")
        pool, _ = _janitor_pool(media, durable=True, publications=False)

        stats = await LocalFileJanitor(pool, download_dir=str(tmp_path), apply_deletions=True).cleanup()

        assert stats["deleted"] == 0 and stats["rejected"] == 1
        assert media.read_bytes() == b"recoverable"

    async def test_active_reader_blocks_deletion_bdd_060(self, tmp_path) -> None:
        media = tmp_path / "synthetic.mp4"
        media.write_bytes(b"streaming")
        pool, _ = _janitor_pool(media, durable=True, publications=True, reader=True)

        stats = await LocalFileJanitor(pool, download_dir=str(tmp_path), apply_deletions=True).cleanup()

        assert stats["deleted"] == 0 and stats["rejected"] == 1
        assert media.read_bytes() == b"streaming"

    async def test_open_execution_blocks_deletion_bdd_132(self, tmp_path) -> None:
        media = tmp_path / "synthetic.mp4"
        media.write_bytes(b"in use")
        pool, _ = _janitor_pool(media, durable=True, publications=True, open_work=True)

        stats = await LocalFileJanitor(pool, download_dir=str(tmp_path), apply_deletions=True).cleanup()

        assert stats["deleted"] == 0 and stats["rejected"] == 1
        assert media.read_bytes() == b"in use"

    async def test_dry_run_changes_nothing_bdd_133(self, tmp_path) -> None:
        media = tmp_path / "synthetic.mp4"
        media.write_bytes(b"eligible")
        pool, conn = _janitor_pool(media, durable=True, publications=True)

        stats = await LocalFileJanitor(pool, download_dir=str(tmp_path)).cleanup()

        assert stats["rejected"] == 1 and stats["deleted"] == 0
        assert media.read_bytes() == b"eligible"
        assert conn.updates == []
        assert conn.audits == []

    async def test_apply_requires_verified_cleanup_contract_bdd_005_131(self, tmp_path) -> None:
        media = tmp_path / "synthetic.mp4"
        media.write_bytes(b"eligible")
        neighbour = tmp_path / "other.mp4"
        neighbour.write_bytes(b"untouched")
        pool, conn = _janitor_pool(media, durable=True, publications=True)

        stats = await LocalFileJanitor(pool, download_dir=str(tmp_path), apply_deletions=True).cleanup()

        assert stats["deleted"] == 0 and stats["rejected"] == 1
        assert media.exists()
        assert neighbour.read_bytes() == b"untouched"
        assert conn.audits and conn.audits[0][3] == "rejected"

    async def test_target_outside_the_root_is_never_unlinked_bdd_061(self, tmp_path) -> None:
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(b"not ours")
        root = tmp_path / "staging"
        root.mkdir()
        pool, conn = _janitor_pool(outside, durable=True, publications=True)

        stats = await LocalFileJanitor(pool, download_dir=str(root), apply_deletions=True).cleanup()

        assert stats["unsafe"] == 1 and stats["deleted"] == 0
        assert outside.read_bytes() == b"not ours"
        assert conn.audits and conn.audits[0][3] == "unsafe"
