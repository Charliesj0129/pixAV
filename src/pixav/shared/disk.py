"""Persistent download-pause guard driven by filesystem capacity."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import redis.asyncio as aioredis

from pixav.shared.metrics import set_disk_free, set_download_paused


@dataclass(frozen=True)
class DiskStatus:
    path: str
    total_bytes: int
    free_bytes: int
    free_percent: float
    below_threshold: bool
    paused: bool = False
    reason: str | None = None


class DownloadSpaceGuard:
    """Latch download dispatch off when disk crosses either hard threshold."""

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        path: str,
        pause_key: str,
        min_free_bytes: int,
        min_free_percent: float,
    ) -> None:
        self._redis = redis
        self._path = Path(path)
        self._pause_key = pause_key
        self._min_free_bytes = max(0, min_free_bytes)
        self._min_free_percent = max(0.0, min_free_percent)

    def inspect(self) -> DiskStatus:
        probe = self._path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        usage = shutil.disk_usage(probe)
        percent = (usage.free / usage.total * 100.0) if usage.total else 0.0
        below = usage.free < self._min_free_bytes or percent < self._min_free_percent
        set_disk_free(str(self._path), usage.free, percent)
        return DiskStatus(
            path=str(self._path),
            total_bytes=usage.total,
            free_bytes=usage.free,
            free_percent=percent,
            below_threshold=below,
        )

    async def check_and_latch(self) -> DiskStatus:
        status = self.inspect()
        raw = await self._redis.get(self._pause_key)
        paused = raw is not None
        reason: str | None = None
        if status.below_threshold and not paused:
            reason = "disk space below download safety threshold"
            payload = {**asdict(status), "reason": reason, "paused_at": datetime.now(timezone.utc).isoformat()}
            await self._redis.set(self._pause_key, json.dumps(payload, sort_keys=True))
            paused = True
        elif paused:
            try:
                saved = json.loads(raw)
                reason = str(saved.get("reason") or "download pause latched")
            except (json.JSONDecodeError, TypeError, AttributeError):
                reason = "download pause latched"
        set_download_paused(paused)
        return DiskStatus(**{**asdict(status), "paused": paused, "reason": reason})

    async def resume(self) -> DiskStatus:
        status = self.inspect()
        if status.below_threshold:
            raise RuntimeError("disk remains below download safety threshold")
        await self._redis.delete(self._pause_key)
        set_download_paused(False)
        return status
