"""Jackett API client for torrent indexer search."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from pixav.shared.exceptions import CrawlError

logger = logging.getLogger(__name__)


class JackettClient:
    """Search torrent indexers via the Jackett API.

    Implements the ``JackettSearcher`` protocol.
    """

    def __init__(self, base_url: str, api_key: str, *, timeout: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    async def search(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Query Jackett's unified endpoint and return normalised results.

        Args:
            query: Search query string.
            limit: Maximum number of results to return.

        Returns:
            List of dicts with keys: title, magnet_uri, size, seeders.

        Raises:
            CrawlError: If the HTTP request fails.
        """
        url = f"{self._base_url}/api/v2.0/indexers/all/results"
        params: dict[str, Any] = {
            "apikey": self._api_key,
            "Query": query,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise CrawlError(f"Jackett returned {exc.response.status_code}: {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise CrawlError(f"Jackett request failed: {exc}") from exc

        results: list[dict[str, Any]] = []
        for item in data.get("Results", [])[:limit]:
            magnet = item.get("MagnetUri") or None
            results.append(
                {
                    "title": item.get("Title", ""),
                    "magnet_uri": magnet,
                    "size": item.get("Size", 0),
                    "seeders": item.get("Seeders", 0),
                }
            )

        logger.info("jackett returned %d results for query=%r", len(results), query)
        return results
