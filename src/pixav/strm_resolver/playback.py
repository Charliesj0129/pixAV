"""Stable managed playback: bytes come only from the verified playback cache.

A video with any RemoteAsset is managed. Its stable URL never falls back to a
share or CDN URL: it is served from the object the playback worker published
after recomputing its SHA-256, or that object is rebuilt from Google Photos on
demand. PostgreSQL decides what may play; the cache only holds bytes.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import asyncpg
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from pixav.shared.media_file import MediaFileChangedError, MediaFileUnsafeError, open_media_file
from pixav.shared.metrics import record_playback_request
from pixav.shared.playback_models import cache_object_path

logger = logging.getLogger(__name__)

MEDIA_TYPE = "video/mp4"
_CHUNK_BYTES = 256 * 1024
_POLL_SECONDS = 1.0
_RETRY_AFTER_SECONDS = 30

# One row for any video id. A video is managed as soon as it has a RemoteAsset,
# whatever that asset's state; only a READY object on a DURABLE copy may play.
_ELIGIBILITY_SQL = """
SELECT EXISTS (SELECT FROM remote_assets WHERE video_id = $1) AS managed,
       ready.id AS playable_asset_id,
       ready.size_bytes
  FROM (SELECT 1) AS one
  LEFT JOIN LATERAL (
        SELECT p.id, p.size_bytes
          FROM playable_assets p
          JOIN remote_assets r ON r.id = p.remote_asset_id
         WHERE p.video_id = $1 AND p.state = 'READY' AND r.state = 'DURABLE'
  ) AS ready ON true
"""

_DEMAND_SQL = """
INSERT INTO playback_cache_requests (playable_asset_id, requested_at, request_count)
VALUES ($1, now(), 1)
ON CONFLICT (playable_asset_id) DO UPDATE
   SET requested_at = now(),
       request_count = playback_cache_requests.request_count + 1
"""


class PlaybackCacheError(Exception):
    """The cache directory itself cannot be read; a rebuild would not help."""


@dataclass(frozen=True)
class Eligibility:
    """What the database allows a managed video to serve right now."""

    playable_asset_id: uuid.UUID | None
    size_bytes: int | None


async def resolve_eligibility(pool: Any, video_id: uuid.UUID) -> Eligibility | None:
    """Return None for a legacy video, which the older resolver path still owns."""
    try:
        row = await pool.fetchrow(_ELIGIBILITY_SQL, video_id)
    except asyncpg.UndefinedTableError:
        # A schema from before managed playback (migration 019) has no managed
        # videos at all, so every video stays on the legacy path.
        return None
    if row is None or row.get("managed") is not True:
        return None
    return Eligibility(playable_asset_id=row.get("playable_asset_id"), size_bytes=row.get("size_bytes"))


async def record_demand(pool: Any, playable_asset_id: uuid.UUID) -> None:
    """Ask the authority for a rebuild; the request itself never materializes anything."""
    await pool.execute(_DEMAND_SQL, playable_asset_id)


def parse_byte_range(value: str, size: int) -> tuple[int, int]:
    """Parse one RFC 7233 byte range, rejecting multipart/invalid requests."""
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("unsupported byte range")
    spec = value.removeprefix("bytes=").strip()
    if "-" not in spec:
        raise ValueError("invalid byte range")
    start_raw, end_raw = spec.split("-", 1)
    try:
        if not start_raw:
            suffix = int(end_raw)
            if suffix <= 0 or size <= 0:
                raise ValueError("invalid suffix range")
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_raw)
            end = size - 1 if not end_raw else min(int(end_raw), size - 1)
            if start < 0 or start >= size or end < start:
                raise ValueError("unsatisfiable byte range")
    except ValueError as exc:
        raise ValueError("invalid byte range") from exc
    return start, end


class PlaybackCache:
    """The resolver's read side of the directory the playback worker publishes into."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def open_reader(self, playable_asset_id: uuid.UUID, size_bytes: int) -> BinaryIO | None:
        """Open the published object under a shared lock, or None when it cannot be served.

        Only a regular file of exactly the READY size is served; its bytes were
        hashed when it was published. The lock lives as long as the handle, so
        eviction waits for this reader, and a crash releases it with the process.
        """
        path = cache_object_path(self.root, playable_asset_id)
        try:
            with open_media_file(path) as source:
                if os.fstat(source.descriptor).st_size != size_bytes:
                    return None
                # A duplicate retains the same shared kernel lock after the
                # directory guard closes, until this reader itself closes.
                handle = source.reader()
                try:
                    os.utime(source.descriptor)
                except OSError:
                    logger.warning("could not record playback cache access")
                return handle
        except FileNotFoundError:
            return None
        except (BlockingIOError, MediaFileChangedError, MediaFileUnsafeError):
            return None
        except OSError as exc:
            if exc.errno not in {errno.ELOOP, errno.ENOTDIR}:
                raise PlaybackCacheError(type(exc).__name__) from exc
            return None

    async def wait_for(self, playable_asset_id: uuid.UUID, size_bytes: int, seconds: float) -> BinaryIO | None:
        """Poll for a rebuilt object until the deadline; the rebuild continues after it."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            handle = await asyncio.to_thread(self.open_reader, playable_asset_id, size_bytes)
            if handle is not None or time.monotonic() >= deadline:
                return handle
            await asyncio.sleep(min(_POLL_SECONDS, max(0.0, deadline - time.monotonic())))


async def _stream_handle(handle: BinaryIO, *, start: int, end: int) -> AsyncIterator[bytes]:
    """Yield exactly bytes [start, end] of a locked object, then release the lock."""
    try:
        offset = start
        while offset <= end:
            chunk = os.pread(handle.fileno(), min(_CHUNK_BYTES, end - offset + 1), offset)
            if not chunk:
                logger.warning("playback cache object ended early at byte %d", offset)
                return
            offset += len(chunk)
            yield chunk
            # Keep the event loop schedulable during a long stream.
            await asyncio.sleep(0)
    finally:
        handle.close()


def _range_response(request: Request, size: int) -> tuple[int, int, int, dict[str, str]]:
    headers = {"Accept-Ranges": "bytes"}
    range_header = request.headers.get("range")
    if not range_header:
        headers["Content-Length"] = str(size)
        return 200, 0, size - 1, headers
    try:
        start, end = parse_byte_range(range_header, size)
    except ValueError as exc:
        record_playback_request("range_unsatisfiable")
        raise HTTPException(
            status_code=416,
            detail="requested byte range is not satisfiable",
            headers={"Content-Range": f"bytes */{size}"},
        ) from exc
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    return 206, start, end, headers


async def serve_managed(request: Request, pool: Any, eligibility: Eligibility) -> Response:
    """Answer GET or HEAD for a managed video from facts first, then from the cache."""
    playable_id, size = eligibility.playable_asset_id, eligibility.size_bytes
    if playable_id is None or size is None:
        record_playback_request("incomplete")
        raise HTTPException(status_code=409, detail="media incomplete: no verified playable object")
    # HEAD and 416 are answered from the READY record alone: they never open,
    # read or rebuild the cache object.
    status_code, start, end, headers = _range_response(request, size)
    if request.method == "HEAD":
        record_playback_request("head")
        return Response(status_code=status_code, headers=headers, media_type=MEDIA_TYPE)

    cache: PlaybackCache = request.app.state.playback_cache
    outcome = "hit"
    try:
        handle = await asyncio.to_thread(cache.open_reader, playable_id, size)
        if handle is None:
            outcome = "cold_hit"
            await record_demand(pool, playable_id)
            handle = await cache.wait_for(playable_id, size, request.app.state.playback_cold_wait_seconds)
    except PlaybackCacheError as exc:
        record_playback_request("cache_unreadable")
        logger.error("playback cache unreadable for %s: %s", playable_id, exc)
        raise HTTPException(status_code=503, detail="playback cache unavailable") from exc
    if handle is None:
        record_playback_request("cold_timeout")
        raise HTTPException(
            status_code=503,
            detail="playback is being prepared from durable storage",
            headers={"Retry-After": str(_RETRY_AFTER_SECONDS)},
        )
    record_playback_request(outcome)
    return StreamingResponse(
        _stream_handle(handle, start=start, end=end),
        status_code=status_code,
        headers=headers,
        media_type=MEDIA_TYPE,
    )
