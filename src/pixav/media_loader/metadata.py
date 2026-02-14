"""Metadata scraping via the Stash GraphQL API."""

from __future__ import annotations

import logging
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
