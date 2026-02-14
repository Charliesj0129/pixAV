"""API routes for strm_resolver."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

router = APIRouter()


@router.get("/resolve/{video_id}")
async def resolve_video(video_id: str) -> JSONResponse:
    """Look up video share_url from DB and resolve to CDN URL.

    Args:
        video_id: The video identifier

    Returns:
        JSON with cdn_url field
    """
    # Stub: Database lookup and CDN resolution not yet implemented
    raise HTTPException(status_code=501, detail="CDN resolution not yet implemented")


@router.get("/stream/{video_id}")
async def stream_video(video_id: str) -> RedirectResponse:
    """Look up video and redirect to CDN streaming URL.

    Args:
        video_id: The video identifier

    Returns:
        302 redirect to CDN URL
    """
    # Stub: Database lookup and CDN resolution not yet implemented
    raise HTTPException(status_code=501, detail="CDN resolution not yet implemented")


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint.

    Returns:
        Status dictionary
    """
    return {"status": "ok"}
