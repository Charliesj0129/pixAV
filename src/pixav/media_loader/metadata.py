"""Metadata scraping via the Stash GraphQL API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx

from pixav.shared.exceptions import CrawlError

logger = logging.getLogger(__name__)

# Stash uses a GraphQL API
_FIND_SCENES_QUERY = """
query FindScenes($filter: FindFilterType!) {
    findScenes(filter: $filter) {
        count
        scenes {
            id
            title
            date
            details
            rating100
            organized
            studio { name }
            tags { name }
            performers { name }
            files { path duration size video_codec width height }
        }
    }
}
"""


class StashMetadataScraper:
    """Metadata scraper implementation using the Stash GraphQL API.

    Implements the ``MetadataScraper`` protocol.
    """

    def __init__(self, base_url: str, *, timeout: int = 15) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def scrape(self, title: str) -> dict[str, Any]:
        """Search Stash for metadata matching a title.

        Args:
            title: Title of the media to search for.

        Returns:
            Dictionary containing scraped metadata, or an empty dict
            with ``{"found": False}`` if nothing matched.

        Raises:
            CrawlError: If the Stash API request fails.
        """
        variables: dict[str, Any] = {
            "filter": {
                "q": title,
                "per_page": 1,
                "sort": "relevance",
                "direction": "DESC",
            }
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/graphql",
                    json={"query": _FIND_SCENES_QUERY, "variables": variables},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise CrawlError(f"Stash returned {exc.response.status_code}: {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise CrawlError(f"Stash request failed: {exc}") from exc

        errors = data.get("errors")
        if errors:
            messages = "; ".join(str(item.get("message", "GraphQL error")) for item in errors if isinstance(item, dict))
            raise CrawlError(f"Stash GraphQL errors: {messages or 'unknown error'}")

        scenes = data.get("data", {}).get("findScenes", {}).get("scenes", [])
        if not scenes:
            logger.debug("no Stash scenes found for %r", title)
            return {"found": False, "title": title}

        scene = scenes[0]
        result: dict[str, Any] = {
            "found": True,
            "stash_id": scene.get("id"),
            "title": scene.get("title", title),
            "date": scene.get("date"),
            "details": scene.get("details"),
            "rating": scene.get("rating100"),
            "studio": scene.get("studio", {}).get("name") if scene.get("studio") else None,
            "tags": [t["name"] for t in scene.get("tags", [])],
            "performers": [p["name"] for p in scene.get("performers", [])],
        }

        # File info
        files = scene.get("files", [])
        if files:
            f = files[0]
            result["file_info"] = {
                "path": f.get("path"),
                "duration": f.get("duration"),
                "size": f.get("size"),
                "codec": f.get("video_codec"),
                "width": f.get("width"),
                "height": f.get("height"),
            }

        logger.info("stash metadata found for %r: stash_id=%s", title, result.get("stash_id"))
        return result


async def probe_media(path: str, *, ffprobe_bin: str = "ffprobe", timeout: int = 30) -> dict[str, Any]:  # noqa: C901
    """Probe every stream; missing dependencies and malformed media fail closed."""
    from pathlib import Path

    from pixav.media_loader.preparation import MediaFacts, hash_file
    from pixav.shared.exceptions import MediaDependencyError, RemuxError

    target = Path(path)
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise RemuxError("symlink in media input path")
    if not target.is_file():
        raise RemuxError("media file does not exist")
    before = target.stat()
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe_bin,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise MediaDependencyError("ffprobe unavailable") from exc
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise MediaDependencyError("ffprobe timed out") from exc
    if proc.returncode != 0:
        raise RemuxError("ffprobe rejected media")
    try:
        raw = json.loads(stdout)
        streams = tuple(
            {
                "kind": s["codec_type"],
                "codec": s["codec_name"],
                "width": s.get("width", 0),
                "height": s.get("height", 0),
            }
            for s in raw["streams"]
        )
        facts = MediaFacts(
            container=raw["format"]["format_name"],
            size_bytes=raw["format"]["size"],
            duration_seconds=raw["format"]["duration"],
            streams=streams,
            sha256=await hash_file(path),
        )
        video = next(s for s in facts.streams if s.kind == "video")
    except (ValueError, TypeError, KeyError, StopIteration) as exc:
        raise RemuxError("invalid ffprobe media facts") from exc
    after = target.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise RemuxError("media changed during inspection")
    if facts.size_bytes != after.st_size or not video.width or not video.height:
        raise RemuxError("invalid media size or video dimensions")
    return {
        **facts.model_dump(mode="json"),
        "path": path,
        "filename": os.path.basename(path),
        "codec": video.codec,
        "width": video.width,
        "height": video.height,
        "resolution": f"{video.width}x{video.height}",
    }
