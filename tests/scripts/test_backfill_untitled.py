"""Regression tests for the metadata-torrent leak in backfill_untitled.

Measured on 2026-08-30: five of nine torrents in the production qBittorrent
client had no corresponding row in `videos`. Three of them were watermark hashes
parked in `metaDL`, and together they held every one of the three active
download slots, so no real torrent could start. This script created them and
never removed them, because cleanup targeted the DB's `info_hash` while the
torrent had been added under the magnet's own btih.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from pixav.media_loader.qbittorrent import MetadataProbe
from scripts.backfill_untitled import _run

MAGNET = "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709&dn=Test"
MAGNET_HASH = "da39a3ee5e6b4b0d3255bfef95601890afd80709"


class _FakePool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.executed: list[tuple[Any, ...]] = []

    async def fetch(self, *_args: Any) -> list[dict[str, Any]]:
        return self._rows

    async def execute(self, *args: Any) -> None:
        self.executed.append(args)

    async def close(self) -> None:
        return None


def _row(info_hash: str | None) -> dict[str, Any]:
    return {
        "id": "00000000-0000-0000-0000-000000000010",
        "title": "Untitled",
        "magnet_uri": MAGNET,
        "info_hash": info_hash,
        "local_path": None,
        "metadata_json": None,
    }


@pytest.fixture
def qbit() -> AsyncMock:
    client = AsyncMock()
    client.fetch_metadata_name.return_value = MetadataProbe("Real Name", True, MAGNET_HASH)
    client.delete_torrent.return_value = None
    return client


async def _run_with(monkeypatch: pytest.MonkeyPatch, pool: _FakePool, qbit: AsyncMock) -> int:
    monkeypatch.setattr("scripts.backfill_untitled.create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("scripts.backfill_untitled.QBitClient", lambda *a, **k: qbit)
    return await _run(apply=False, limit=10, report_path=None)


@pytest.mark.parametrize(
    ("db_info_hash", "case"),
    [
        (None, "null info_hash"),
        ("", "empty info_hash"),
        ("0000000000000000000000000000000000000000", "stale info_hash disagreeing with the magnet"),
    ],
)
async def test_created_torrent_is_always_cleaned_up_by_the_hash_actually_added(
    monkeypatch: pytest.MonkeyPatch, qbit: AsyncMock, db_info_hash: str | None, case: str
) -> None:
    pool = _FakePool([_row(db_info_hash)])

    await _run_with(monkeypatch, pool, qbit)

    qbit.delete_torrent.assert_awaited_once_with(MAGNET_HASH, delete_files=False)


async def test_pre_existing_torrents_are_never_removed(monkeypatch: pytest.MonkeyPatch, qbit: AsyncMock) -> None:
    """created=False means the torrent was already there; it is not ours to delete."""
    qbit.fetch_metadata_name.return_value = MetadataProbe("Real Name", False, MAGNET_HASH)
    pool = _FakePool([_row(MAGNET_HASH)])

    await _run_with(monkeypatch, pool, qbit)

    qbit.delete_torrent.assert_not_awaited()


async def test_cleanup_still_runs_when_metadata_lookup_raises(monkeypatch: pytest.MonkeyPatch, qbit: AsyncMock) -> None:
    """A lookup failure must not crash the run.

    Cleanup on that path belongs to the adapter, which removes a torrent it
    created before raising (see test_qbittorrent.py), so the script has no hash
    to act on and must simply record the row as unresolved.
    """
    qbit.fetch_metadata_name.side_effect = RuntimeError("qBittorrent timed out")
    pool = _FakePool([_row(None)])

    await _run_with(monkeypatch, pool, qbit)

    qbit.delete_torrent.assert_not_awaited()
