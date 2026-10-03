"""Eviction of the disposable playback cache (BDD-079, 080).

An object is removed when its playable asset is no longer READY on a DURABLE
copy, when nobody has read it for the TTL, or -- oldest first -- while the cache
is over its optional byte budget. Removal first takes an exclusive,
non-blocking lock, so an object a stream still holds is skipped until a later
sweep. Only names this cache creates are removed, and no database row changes:
the cache is never domain truth.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import re
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pixav.shared.metrics import record_playback_cache_eviction
from pixav.shared.playback_models import CACHE_OBJECTS

logger = logging.getLogger(__name__)

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_OBJECT_NAME = re.compile(rf"({_UUID})\.mp4")
# A publication links under this name and renames it in the same breath; one
# still present after the grace period was left by a crash between the two.
_PARTIAL_NAME = re.compile(rf"\.{_UUID}\.partial")
_PARTIAL_GRACE_SECONDS = 3600

_STATES_SQL = """
SELECT p.id, p.state, r.state AS remote_state
  FROM playable_assets p
  JOIN remote_assets r ON r.id = p.remote_asset_id
 WHERE p.id = ANY($1::uuid[])
"""


@dataclass(frozen=True)
class CacheEntry:
    playable_asset_id: uuid.UUID
    name: str
    size: int
    last_access: float


def _evict(path: Path) -> bool:
    """Unlink one regular file only while no reader holds it."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        facts = os.fstat(descriptor)
        if not stat.S_ISREG(facts.st_mode):
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        # A republish may have replaced the name since it was opened; only the
        # locked object itself may be removed.
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) != (facts.st_dev, facts.st_ino):
            return False
        os.unlink(path)
        return True
    finally:
        os.close(descriptor)


class PlaybackCacheJanitor:
    def __init__(
        self,
        pool: Any,
        root: Path,
        *,
        ttl_seconds: float,
        max_bytes: int = 0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.pool = pool
        self.objects = root / CACHE_OBJECTS
        self.ttl_seconds = ttl_seconds
        self.max_bytes = max_bytes
        self._clock = clock

    async def sweep(self) -> dict[str, int]:
        """Evict what the policy allows now; return evictions by reason."""
        entries, partials = await asyncio.to_thread(self._scan)
        states = await self._states([entry.playable_asset_id for entry in entries])
        planned = [(entry.name, reason) for entry, reason in self._plan(entries, states)]
        planned += [(name, "orphan") for name in partials]
        evicted = await asyncio.to_thread(self._evict_planned, planned)
        for reason, count in evicted.items():
            for _ in range(count):
                record_playback_cache_eviction(reason)
        return evicted

    def _scan(self) -> tuple[list[CacheEntry], list[str]]:
        try:
            names = os.listdir(self.objects)
        except FileNotFoundError:
            return [], []
        entries: list[CacheEntry] = []
        partials: list[str] = []
        now = self._clock()
        for name in names:
            try:
                facts = os.lstat(self.objects / name)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(facts.st_mode):
                continue
            match = _OBJECT_NAME.fullmatch(name)
            if match:
                last_access = max(facts.st_atime, facts.st_mtime)
                entries.append(CacheEntry(uuid.UUID(match.group(1)), name, facts.st_size, last_access))
            elif _PARTIAL_NAME.fullmatch(name) and now - facts.st_ctime > _PARTIAL_GRACE_SECONDS:
                partials.append(name)
        return entries, partials

    async def _states(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[str, str]]:
        if not ids:
            return {}
        rows = await self.pool.fetch(_STATES_SQL, ids)
        return {row["id"]: (row["state"], row["remote_state"]) for row in rows}

    def _plan(
        self, entries: list[CacheEntry], states: dict[uuid.UUID, tuple[str, str]]
    ) -> list[tuple[CacheEntry, str]]:
        now = self._clock()
        doomed: list[tuple[CacheEntry, str]] = []
        kept: list[CacheEntry] = []
        for entry in entries:
            state = states.get(entry.playable_asset_id)
            if state is None:
                doomed.append((entry, "orphan"))
            elif state[0] == "PREPARING":
                # Published but not yet accepted by the authority: evicting it
                # now would discard a finished materialization.
                continue
            elif state != ("READY", "DURABLE"):
                doomed.append((entry, "not_ready"))
            elif now - entry.last_access > self.ttl_seconds:
                doomed.append((entry, "expired"))
            else:
                kept.append(entry)
        if self.max_bytes > 0:
            total = sum(entry.size for entry in kept)
            for entry in sorted(kept, key=lambda item: item.last_access):
                if total <= self.max_bytes:
                    break
                doomed.append((entry, "over_budget"))
                total -= entry.size
        return doomed

    def _evict_planned(self, planned: list[tuple[str, str]]) -> dict[str, int]:
        evicted: dict[str, int] = {}
        for name, reason in planned:
            if _evict(self.objects / name):
                evicted[reason] = evicted.get(reason, 0) + 1
            else:
                logger.info("playback cache object %s kept for now (%s): in use or replaced", name, reason)
        if evicted:
            descriptor = os.open(self.objects, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return evicted


async def run_janitor(janitor: PlaybackCacheJanitor, interval_seconds: float) -> None:
    """Sweep forever; one failed sweep is logged and never stops playback."""
    while True:
        try:
            await janitor.sweep()
        except Exception:
            logger.exception("playback cache sweep failed")
        await asyncio.sleep(interval_seconds)
