"""Persistent download-pause guard driven by filesystem capacity."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import stat
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast

import redis.asyncio as aioredis

from pixav.shared.metrics import set_disk_free, set_download_paused

if TYPE_CHECKING:
    from pixav.config import Settings


@dataclass(frozen=True)
class PhysicalCapacity:
    path: str
    total_bytes: int
    free_bytes: int

    def __post_init__(self) -> None:
        if self.total_bytes <= 0 or not 0 <= self.free_bytes <= self.total_bytes:
            raise ValueError("invalid physical capacity")


@dataclass(frozen=True)
class DiskStatus:
    path: str
    total_bytes: int
    free_bytes: int
    free_percent: float
    below_threshold: bool
    paused: bool = False
    reason: str | None = None
    physical: PhysicalCapacity | None = None


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
        owner_token: str | None = None,
        physical_probe: Callable[[], Awaitable[PhysicalCapacity]] | None = None,
    ) -> None:
        self._redis = redis
        self._path = Path(path)
        self._pause_key = pause_key
        self._min_free_bytes = max(0, min_free_bytes)
        self._min_free_percent = max(0.0, min_free_percent)
        if owner_token is not None and len(owner_token) < 32:
            raise ValueError("download pause owner token is too short")
        self._owner_hash = hashlib.sha256(owner_token.encode()).hexdigest() if owner_token else None
        self._physical_probe = physical_probe

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

    async def _inspect_all(self) -> DiskStatus:
        status = self.inspect()
        if self._physical_probe is None:
            return status
        try:
            physical = await self._physical_probe()
        except Exception:
            # Native/TLS errors may contain private diagnostics; persist only a
            # fixed reason. Unknown physical capacity is never safe to resume.
            return replace(status, below_threshold=True, reason="physical disk capacity unavailable")
        percent = physical.free_bytes / physical.total_bytes * 100
        below = physical.free_bytes < self._min_free_bytes or percent < self._min_free_percent
        set_disk_free("windows:" + physical.path, physical.free_bytes, percent)
        return replace(
            status,
            physical=physical,
            below_threshold=status.below_threshold or below,
            reason="physical disk below download safety threshold" if below else status.reason,
        )

    async def status(self) -> DiskStatus:
        """Observe both filesystems and the existing latch without writing."""
        status = await self._inspect_all()
        raw = await self._redis.get(self._pause_key)
        paused = raw is not None
        reason = status.reason
        if paused and reason is None:
            reason = "download pause latched"
        set_download_paused(paused)
        return replace(status, paused=paused, reason=reason)

    async def check_and_latch(self) -> DiskStatus:
        status = await self.status()
        paused, reason = status.paused, status.reason
        if status.below_threshold and not paused:
            reason = reason or "disk space below download safety threshold"
            payload = {
                **asdict(status),
                "paused": True,
                "reason": reason,
                "paused_at": datetime.now(timezone.utc).isoformat(),
                "owner_sha256": self._owner_hash,
            }
            # A concurrent operator's pause always wins; no overwrite or TTL.
            await self._redis.set(self._pause_key, json.dumps(payload, sort_keys=True), nx=True)
            paused = True
        set_download_paused(paused)
        return replace(status, paused=paused, reason=reason)

    async def resume(self) -> DiskStatus:
        status = await self._inspect_all()
        if status.below_threshold:
            raise RuntimeError("disk remains below download safety threshold")
        raw = await self._redis.get(self._pause_key)
        if raw is None:
            return status
        try:
            owner = json.loads(raw).get("owner_sha256")
        except (ValueError, TypeError, AttributeError):
            owner = None
        if not self._owner_hash or not isinstance(owner, str) or not hmac.compare_digest(owner, self._owner_hash):
            raise RuntimeError("download pause owner token does not match")
        removed = await cast(
            Awaitable[int],
            self._redis.eval(
                "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end",
                1,
                self._pause_key,
                raw,
            ),
        )
        if not removed:
            raise RuntimeError("download pause changed during resume")
        set_download_paused(False)
        return status


def read_pause_token(path: str) -> str:
    """Read one private file without following a final-component symlink."""
    if not Path(path).is_absolute() or Path(path).resolve() != Path(path):
        raise ValueError("download pause token requires an absolute private file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096:
            raise ValueError("download pause token requires an exact 0600 file")
        token = handle.read().rstrip("\r\n")
    if len(token) < 32:
        raise ValueError("download pause token is too short")
    return token


def build_download_space_guard(settings: Settings, redis: aioredis.Redis) -> DownloadSpaceGuard:
    probe = None
    if settings.pixel_injector_mode == "windows_android":
        from pixav.pixel_injector.windows_control import WindowsCapacityProbe

        if not settings.download_pause_token_file:
            raise ValueError("Windows disk guard requires a persistent pause owner token file")
        probe = WindowsCapacityProbe(settings)
    return DownloadSpaceGuard(
        redis,
        path=settings.download_dir,
        pause_key=settings.download_pause_key,
        min_free_bytes=settings.download_min_free_bytes,
        min_free_percent=settings.download_min_free_percent,
        owner_token=(
            read_pause_token(settings.download_pause_token_file) if settings.download_pause_token_file else None
        ),
        physical_probe=probe,
    )
