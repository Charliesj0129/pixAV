"""Jackett API client for torrent indexer search."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from pixav.shared.exceptions import CrawlError
from pixav.shared.watermark import is_watermark_info_hash
from pixav.sht_probe.interfaces import IndexerResult

logger = logging.getLogger(__name__)


class JackettClient:
    """Search torrent indexers via the Jackett API.

    Implements the ``JackettSearcher`` protocol.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: int = 30,
        indexer: str = "all",
        resolve_download_links: bool = False,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", indexer) or (resolve_download_links and indexer == "all"):
            raise ValueError("download resolution requires a specific valid indexer")
        self._indexer = indexer
        self._resolve_download_links = resolve_download_links

    async def search(self, query: str, *, limit: int = 50) -> list[IndexerResult]:
        """Query Jackett's unified endpoint and return normalised results.

        Args:
            query: Search query string.
            limit: Maximum number of results to return.

        Returns:
            List of dicts with keys: title, magnet_uri, source_url, size,
            seeders. Numeric scoring inputs are normalized to non-negative
            integers at this adapter boundary.

        Raises:
            CrawlError: If the HTTP request fails.
        """
        url = f"{self._base_url}/api/v2.0/indexers/{self._indexer}/results"
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
            raise CrawlError(f"Jackett returned {exc.response.status_code}") from None
        except httpx.HTTPError:
            raise CrawlError("Jackett request failed") from None
        except ValueError:
            raise CrawlError("Jackett response invalid JSON") from None

        raw_results = data.get("Results", []) if isinstance(data, dict) else []
        if not isinstance(raw_results, list):
            raise CrawlError("Jackett response Results is not a list")

        results: list[IndexerResult] = []
        for item in raw_results[: max(0, limit)]:
            if not isinstance(item, dict):
                continue
            magnet_raw = item.get("MagnetUri")
            magnet = str(magnet_raw).strip() if magnet_raw else None
            if magnet is None and self._resolve_download_links:
                magnet = await self._resolve_link(item.get("Link"))
            details_raw = item.get("Details") or item.get("Guid")
            results.append(
                {
                    "title": str(item.get("Title") or ""),
                    "magnet_uri": magnet,
                    "source_url": str(details_raw or ""),
                    "size": _nonnegative_int(item.get("Size")),
                    "seeders": _nonnegative_int(item.get("Seeders")),
                }
            )

        logger.info("jackett returned %d results for query=%r", len(results), query)
        return results

    async def _resolve_link(self, link: Any) -> str:
        """Accept only the observed Jackett 302 magnet download contract."""
        try:
            target, origin = urlsplit(str(link or "")), urlsplit(self._base_url)
            if (
                (target.scheme, target.hostname, target.port) != (origin.scheme, origin.hostname, origin.port)
                or target.username is not None
                or target.password is not None
                or target.fragment
                or target.path != f"{origin.path}/dl/{self._indexer}/"
                or set(parse_qs(target.query)) != {"jackett_apikey", "path", "file"}
                or any(len(values) != 1 for values in parse_qs(target.query).values())
                or parse_qs(target.query)["jackett_apikey"] != [self._api_key]
            ):
                raise ValueError
        except ValueError:
            raise CrawlError("Jackett download destination rejected") from None
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                async with client.stream("GET", str(link)) as response:
                    location = response.headers.get("Location", "")
                    parsed = urlsplit(location)
                    xt = parse_qs(parsed.query).get("xt", [])
                    if response.status_code != 302 or parsed.scheme != "magnet" or len(xt) != 1:
                        raise CrawlError("Jackett download contract mismatch")
                    match = re.fullmatch(r"urn:btih:([a-fA-F0-9]{40})", xt[0])
                    if not match or is_watermark_info_hash(match[1]):
                        raise CrawlError("Jackett download invalid info hash")
                    return "magnet:?xt=urn:btih:" + match[1].lower()
        except httpx.HTTPError:
            raise CrawlError("Jackett download request failed") from None
        except ValueError:
            raise CrawlError("Jackett download contract mismatch") from None


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
