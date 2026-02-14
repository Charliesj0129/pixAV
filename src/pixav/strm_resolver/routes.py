"""API routes for strm_resolver."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from pixav.shared.exceptions import ResolveError
from pixav.strm_resolver.cache import CdnCache

router = APIRouter()


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


def _get_resolver(request: Request) -> Any:
    resolver = _state(request, "resolver")
    if resolver is None or not hasattr(resolver, "resolve"):
        raise HTTPException(status_code=503, detail="resolver unavailable")
    return resolver


async def _resolve_cdn(request: Request, video_id: str) -> tuple[str, str]:
    """Resolve CDN URL and return tuple (cdn_url, source)."""
    parsed_video_id = _parse_uuid(video_id)
    db_pool = _get_db_pool(request)

    row = await db_pool.fetchrow(
        """
        SELECT id, share_url, cdn_url
          FROM videos
         WHERE id = $1
        """,
        parsed_video_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="video not found")

    cache = _get_cache(request)

    if cache is not None:
        cached = await cache.get(video_id)
        if cached:
            return cached, "cache"

    db_cdn_url = row.get("cdn_url")
    if isinstance(db_cdn_url, str) and db_cdn_url:
        if cache is not None:
            await cache.set(video_id, db_cdn_url)
        return db_cdn_url, "database"

    share_url = row.get("share_url")
    if not isinstance(share_url, str) or not share_url:
        raise HTTPException(status_code=409, detail="video is not uploaded yet (share_url missing)")

    resolver = _get_resolver(request)
    try:
        cdn_url = await resolver.resolve(share_url)
    except ResolveError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await db_pool.execute(
        """
        UPDATE videos
           SET cdn_url = $1,
               status = 'available',
               updated_at = now()
         WHERE id = $2
        """,
        cdn_url,
        parsed_video_id,
    )
    if cache is not None:
        await cache.set(video_id, cdn_url)
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


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint.

    Returns:
        Status dictionary
    """
    return {"status": "ok"}
