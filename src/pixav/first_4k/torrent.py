"""qBittorrent client for the isolated run, with the disk latch applied per poll."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import redis.asyncio as aioredis

from pixav.media_loader.qbittorrent import QBitClient
from pixav.media_loader.video_parts import require_space
from pixav.pixel_injector.canary import CanaryBlockedError
from pixav.shared.disk import DownloadSpaceGuard

from .settings import REDIS, WORK


class MovieTorrent(QBitClient):
    """Apply the existing disk latch to each isolated qBit progress observation."""

    check: Callable[[], None] | None = None
    progress: Callable[[], None] | None = None
    _owned_hash: str | None = None

    async def stop_owned(self) -> None:
        if self._owned_hash:
            response = await self._request("POST", "/api/v2/torrents/stop", data={"hashes": self._owned_hash})
            if response.status_code == 404:
                response = await self._request("POST", "/api/v2/torrents/pause", data={"hashes": self._owned_hash})
            response.raise_for_status()

    async def __aenter__(self) -> MovieTorrent:
        # QBitClient.__aenter__ is annotated as returning QBitClient, so
        # `async with MovieTorrent(...)` would lose this subclass and with it
        # the check/progress latches. Same object, narrower type.
        await super().__aenter__()
        return self

    # The supertype takes *_exc_info; three named arguments are the protocol's
    # own shape and what `async with` passes, but mypy sees a narrowing.
    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:  # type: ignore[override]
        try:
            if exc_type is not None:
                await asyncio.wait_for(self.stop_owned(), 15)
        finally:
            await super().__aexit__(exc_type, exc_val, exc_tb)

    async def _enforce_size_limit(self, info: Any, torrent_hash: str) -> None:
        if info.get("hash") != torrent_hash or str(info.get("save_path", "")).rstrip("/") != self._download_dir.rstrip(
            "/"
        ):
            raise CanaryBlockedError("torrent identity or owned download path changed")
        self._owned_hash = torrent_hash
        if self.check:
            self.check()
        await super()._enforce_size_limit(info, torrent_hash)
        # amount_left is conservative for preallocated files; completed bytes
        # are already charged to this filesystem and are not reserved again.
        remaining = int(info.get("amount_left") or 0)
        if not info.get("total_size"):
            remaining = self._max_download_bytes or 0
        require_space([(WORK / "downloads" / torrent_hash, remaining)])
        if self.progress:
            self.progress()
        redis = aioredis.from_url(REDIS)
        try:
            guard = DownloadSpaceGuard(
                redis,
                path=str(WORK / "downloads"),
                pause_key="first-4k:disk:pause",
                min_free_bytes=100 * 1024**3,
                min_free_percent=10,
            )
            if (await guard.check_and_latch()).paused:
                stopped = await self._request("POST", "/api/v2/torrents/stop", data={"hashes": torrent_hash})
                if stopped.status_code == 404:
                    stopped = await self._request("POST", "/api/v2/torrents/pause", data={"hashes": torrent_hash})
                stopped.raise_for_status()
                raise CanaryBlockedError("isolated disk pause latched; torrent stopped and retained")
        finally:
            await redis.aclose()
