"""API routes for strm_resolver."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse

from pixav.shared.exceptions import ResolveError
from pixav.shared.metrics import get_metrics_output
from pixav.strm_resolver.cache import CdnCache

router = APIRouter()

_FILE_CHUNK_BYTES = 64 * 1024


def _parse_uuid(video_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(video_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid video_id: must be UUID") from exc


def _state(request: Request, key: str) -> Any:
    return getattr(request.app.state, key, None)


def _get_db_pool(request: Request) -> Any:
    db_pool = _state(request, "db_pool")
    if db_pool is None or not hasattr(db_pool, "fetchrow") or not hasattr(db_pool, "execute"):
        raise HTTPException(status_code=503, detail="database unavailable")
    return db_pool


def _get_cache(request: Request) -> CdnCache | None:
    redis_client = _state(request, "redis")
    if redis_client is None:
        return None
    return CdnCache(redis_client)


async def _cache_get(cache: CdnCache | None, video_id: str) -> str | None:
    if cache is None:
        return None
    return await cache.get(video_id)


async def _cache_set(cache: CdnCache | None, video_id: str, cdn_url: str) -> None:
    if cache is None:
        return
    await cache.set(video_id, cdn_url)


def _get_resolver(request: Request) -> Any:
    resolver = _state(request, "resolver")
    if resolver is None or not hasattr(resolver, "resolve"):
        raise HTTPException(status_code=503, detail="resolver unavailable")
    return resolver


def _local_share_scheme(request: Request) -> str:
    raw = _state(request, "local_share_scheme")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return "pixav-local://"


def _parse_byte_range(value: str, size: int) -> tuple[int, int]:
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


async def _stream_file(path: str, *, start: int, end: int) -> AsyncIterator[bytes]:
    """Yield a bounded local-file segment without relying on anyio's worker pool."""
    remaining = max(0, end - start + 1)
    with open(path, "rb") as handle:  # noqa: PTH123 - path is a DB-backed media path
        handle.seek(start)
        while remaining:
            chunk = handle.read(min(_FILE_CHUNK_BYTES, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
            # Keep the resolver event-loop heartbeat schedulable during large streams.
            await asyncio.sleep(0)


async def _resolve_cdn(request: Request, video_id: str) -> tuple[str, str]:
    """Resolve CDN URL and return tuple (cdn_url, source)."""
    parsed_video_id = _parse_uuid(video_id)

    # Domain readiness precedes cache: a stale first-part URL must never win.
    cache = _get_cache(request)

    # 2. Query DB on Cache Miss
    db_pool = _get_db_pool(request)
    row = await db_pool.fetchrow(
        """
        SELECT id, share_url, local_path, manifest_version, playback_manifest_version, metadata_json
          FROM videos
         WHERE id = $1
        """,
        parsed_video_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="video not found")

    if row.get("manifest_version") is not None:
        _prepared_part_path(row)
        return f"{str(request.base_url).rstrip('/')}/local/{video_id}", "local"

    cached = await _cache_get(cache, video_id)
    if cached:
        return cached, "cache"

    # 3. Re-resolve from share_url. The database deliberately holds no cdn_url:
    # a Google Photos CDN URL is signed for about an hour, and a persisted copy
    # has no expiry, so reading one back would let an expired URL outlive the
    # Redis TTL that exists precisely to bound it — permanently, since each read
    # would refresh the cache with the dead value.
    share_url = row.get("share_url")
    if not isinstance(share_url, str) or not share_url:
        raise HTTPException(status_code=409, detail="video is not uploaded yet (share_url missing)")

    # 3a. Local synthetic share_url scheme (dev/test mode).
    local_scheme = _local_share_scheme(request)
    if share_url.startswith(local_scheme):
        base_url = str(request.base_url).rstrip("/")
        cdn_url = f"{base_url}/local/{video_id}"
        await db_pool.execute(
            """
            UPDATE videos
               SET status = 'available',
                   updated_at = now()
             WHERE id = $1
            """,
            parsed_video_id,
        )
        await _cache_set(cache, video_id, cdn_url)
        return cdn_url, "local"

    resolver = _get_resolver(request)
    try:
        cdn_url = await resolver.resolve(share_url)
    except ResolveError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await db_pool.execute(
        """
        UPDATE videos
           SET status = 'available',
               updated_at = now()
         WHERE id = $1
        """,
        parsed_video_id,
    )
    await _cache_set(cache, video_id, cdn_url)
    return cdn_url, "resolved"


@router.get("/resolve/{video_id}")
async def resolve_video(video_id: str, request: Request) -> dict[str, str]:
    """Resolve video share URL to CDN URL."""
    cdn_url, source = await _resolve_cdn(request, video_id)
    return {"video_id": video_id, "cdn_url": cdn_url, "source": source}


@router.get("/stream/{video_id}")
async def stream_video(video_id: str, request: Request) -> RedirectResponse:
    """Resolve then redirect to CDN URL."""
    cdn_url, _source = await _resolve_cdn(request, video_id)
    return RedirectResponse(url=cdn_url, status_code=302)


def _prepared_part_path(row: Any) -> str:
    path = row.get("local_path")
    metadata = row.get("metadata_json") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    evidence = metadata.get("segmented_playback", {})
    if (
        row.get("playback_manifest_version") != row.get("manifest_version")
        or not isinstance(path, str)
        or not path
        or not os.path.isfile(path)
        or os.path.islink(path)
        or evidence.get("content") != "PASS"
        or evidence.get("cold_inputs") != "photos-only"
        or os.path.getsize(path) != evidence.get("size")
    ):
        raise HTTPException(status_code=409, detail="segmented playback requires prepare-playback")
    return path


@router.api_route("/local/{video_id}", methods=["GET", "HEAD"])
async def local_video(video_id: str, request: Request) -> StreamingResponse:
    """Serve the locally downloaded/remuxed file for a video.

    This is primarily intended for dev/test pipelines where the upload stage
    produces a synthetic share_url and strm_resolver points back to this host.
    """
    parsed_video_id = _parse_uuid(video_id)
    db_pool = _get_db_pool(request)
    row = await db_pool.fetchrow(
        """
        SELECT id, local_path, manifest_version, playback_manifest_version, metadata_json
          FROM videos
         WHERE id = $1
        """,
        parsed_video_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="video not found")

    local_path = row.get("local_path")
    if row.get("manifest_version") is not None:
        local_path = _prepared_part_path(row)
    if not isinstance(local_path, str) or not local_path:
        raise HTTPException(status_code=409, detail="video local_path missing")
    if not os.path.isfile(local_path):
        raise HTTPException(status_code=404, detail="local file not found on server")

    size = os.path.getsize(local_path)
    range_header = request.headers.get("range")
    status_code = 200
    start = 0
    end = size - 1
    headers = {"Accept-Ranges": "bytes"}
    if range_header:
        try:
            start, end = _parse_byte_range(range_header, size)
        except ValueError as exc:
            raise HTTPException(
                status_code=416,
                detail="requested byte range is not satisfiable",
                headers={"Content-Range": f"bytes */{size}"},
            ) from exc
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(max(0, end - start + 1))
    media_type = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    return StreamingResponse(
        _stream_file(local_path, start=start, end=-1 if request.method == "HEAD" else end),
        status_code=status_code,
        headers=headers,
        media_type=media_type,
    )


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint.

    Returns:
        Status dictionary
    """
    return {"status": "ok", "module": "strm_resolver"}


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> PlainTextResponse:
    """Prometheus text-format metrics, matching the shared worker contract."""
    return PlainTextResponse(
        content=get_metrics_output().decode("utf-8"),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
