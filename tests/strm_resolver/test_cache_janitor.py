"""Playback cache eviction (BDD-079, 080).

Real files and real locks; the database is a fake that can only be read, so a
sweep that tried to change a domain row would fail loudly.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from pixav.shared.playback_models import cache_object_path
from pixav.strm_resolver.cache_janitor import PlaybackCacheJanitor
from pixav.strm_resolver.playback import PlaybackCache

NOW = 1_900_000_000.0
HOUR = 3600.0


class ReadOnlyPool:
    def __init__(self) -> None:
        self.states: dict[uuid.UUID, tuple[str, str]] = {}

    async def fetch(self, sql: str, ids):
        assert sql.lstrip().startswith("SELECT"), sql
        return [
            {"id": key, "state": state, "remote_state": remote}
            for key, (state, remote) in self.states.items()
            if key in ids
        ]


@pytest.fixture
def pool() -> ReadOnlyPool:
    return ReadOnlyPool()


def janitor(pool, root: Path, *, ttl_hours: float = 72, max_bytes: int = 0) -> PlaybackCacheJanitor:
    return PlaybackCacheJanitor(pool, root, ttl_seconds=ttl_hours * HOUR, max_bytes=max_bytes, clock=lambda: NOW)


def cached(pool, root: Path, *, state=("READY", "DURABLE"), age_hours: float = 1, size: int = 10) -> Path:
    playable_id = uuid.uuid4()
    if state is not None:
        pool.states[playable_id] = state
    path = cache_object_path(root, playable_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    moment = NOW - age_hours * HOUR
    os.utime(path, (moment, moment))
    return path


async def test_unread_object_expires_and_a_fresh_one_stays(pool, tmp_path):
    old = cached(pool, tmp_path, age_hours=73)
    fresh = cached(pool, tmp_path, age_hours=71)

    evicted = await janitor(pool, tmp_path).sweep()

    assert evicted == {"expired": 1}
    assert not old.exists() and fresh.exists()


async def test_expired_object_waits_for_its_reader_bdd_080(pool, tmp_path):
    path = cached(pool, tmp_path, age_hours=100)
    playable_id = uuid.UUID(path.stem)
    handle = PlaybackCache(tmp_path).open_reader(playable_id, 10)
    # Opening records an access; age it again so only the lock protects it.
    os.utime(path, (NOW - 100 * HOUR, NOW - 100 * HOUR))

    assert await janitor(pool, tmp_path).sweep() == {}
    assert path.exists()

    handle.close()
    assert await janitor(pool, tmp_path).sweep() == {"expired": 1}
    assert not path.exists()


async def test_opening_an_object_counts_as_access(pool, tmp_path):
    path = cached(pool, tmp_path, age_hours=100)
    PlaybackCache(tmp_path).open_reader(uuid.UUID(path.stem), 10).close()

    sweeper = PlaybackCacheJanitor(pool, tmp_path, ttl_seconds=72 * HOUR)
    assert await sweeper.sweep() == {}


@pytest.mark.parametrize("state", [("STALE", "DURABLE"), ("INVALID", "DURABLE"), ("READY", "INVALID")])
async def test_object_that_may_no_longer_play_is_evicted(pool, tmp_path, state):
    path = cached(pool, tmp_path, state=state)

    assert await janitor(pool, tmp_path).sweep() == {"not_ready": 1}
    assert not path.exists()


async def test_published_object_awaiting_acceptance_is_kept(pool, tmp_path):
    """Between publication and the authority's decision the row is still PREPARING."""
    path = cached(pool, tmp_path, state=("PREPARING", "DURABLE"), age_hours=1000)

    assert await janitor(pool, tmp_path).sweep() == {}
    assert path.exists()


async def test_object_with_no_row_is_an_orphan(pool, tmp_path):
    path = cached(pool, tmp_path, state=None)

    assert await janitor(pool, tmp_path).sweep() == {"orphan": 1}
    assert not path.exists()


async def test_over_budget_evicts_least_recently_read_first(pool, tmp_path):
    oldest = cached(pool, tmp_path, age_hours=30, size=40)
    middle = cached(pool, tmp_path, age_hours=20, size=40)
    newest = cached(pool, tmp_path, age_hours=10, size=40)

    assert await janitor(pool, tmp_path, max_bytes=90).sweep() == {"over_budget": 1}
    assert not oldest.exists() and middle.exists() and newest.exists()


async def test_only_names_the_cache_creates_are_touched(pool, tmp_path):
    objects = tmp_path / "objects"
    objects.mkdir()
    foreign = objects / "notes.txt"
    foreign.write_text("operator file")
    directory = objects / f"{uuid.uuid4()}.mp4"
    directory.mkdir()
    target = tmp_path / "target.mp4"
    target.write_bytes(b"y")
    link = objects / f"{uuid.uuid4()}.mp4"
    link.symlink_to(target)
    for path in (foreign, target):
        os.utime(path, (0, 0))

    assert await janitor(pool, tmp_path).sweep() == {}
    assert foreign.exists() and directory.is_dir() and link.is_symlink() and target.exists()


async def test_crash_leftover_partial_is_removed_after_grace(pool, tmp_path):
    objects = tmp_path / "objects"
    objects.mkdir()
    partial = objects / f".{uuid.uuid4()}.partial"
    partial.write_bytes(b"z")
    sweeper = janitor(pool, tmp_path)

    sweeper._clock = lambda: os.lstat(partial).st_ctime + 60
    assert await sweeper.sweep() == {}, "a partial younger than the grace period may be mid-publication"

    sweeper._clock = lambda: os.lstat(partial).st_ctime + 2 * HOUR
    assert await sweeper.sweep() == {"orphan": 1}
    assert not partial.exists()


async def test_missing_cache_directory_is_an_empty_sweep(pool, tmp_path):
    assert await janitor(pool, tmp_path / "absent").sweep() == {}


async def test_deleting_the_cache_changes_no_domain_row_bdd_079(pool, tmp_path):
    """The pool can only SELECT; eviction of every object leaves the facts alone."""
    for state in (("READY", "DURABLE"), ("STALE", "DURABLE")):
        cached(pool, tmp_path, state=state, age_hours=500)
    before = dict(pool.states)

    await janitor(pool, tmp_path).sweep()

    assert pool.states == before
    assert list((tmp_path / "objects").iterdir()) == []


async def test_republished_object_is_not_evicted_using_the_previous_inode_plan(pool, tmp_path):
    path = cached(pool, tmp_path, age_hours=100)
    fetch = pool.fetch

    async def replace_after_scan(sql, ids):
        replacement = tmp_path / "new-publication.mp4"
        replacement.write_bytes(b"fresh-copy")
        os.utime(replacement, (NOW, NOW))
        replacement.replace(path)
        return await fetch(sql, ids)

    pool.fetch = replace_after_scan
    assert await janitor(pool, tmp_path).sweep() == {}
    assert path.read_bytes() == b"fresh-copy"


def test_reader_rejects_a_name_replaced_before_its_shared_lock(tmp_path, monkeypatch):
    import fcntl
    import stat

    playable = uuid.uuid4()
    path = cache_object_path(tmp_path, playable)
    path.parent.mkdir()
    path.write_bytes(b"old-copy")
    lock = fcntl.flock

    def replace_before_lock(descriptor, operation):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            replacement = tmp_path / "replacement.mp4"
            replacement.write_bytes(b"new-copy")
            replacement.replace(path)
        lock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", replace_before_lock)
    reader = PlaybackCache(tmp_path).open_reader(playable, 8)
    if reader is not None:
        reader.close()
    assert reader is None
    assert path.read_bytes() == b"new-copy"
