"""Interfaces for SHT-Probe module."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ContentCrawler(Protocol):
    """Protocol for content discovery crawlers."""

    async def crawl(self, url: str) -> list[str]:
        """Crawl a URL and return list of discovered page URLs.

        Args:
            url: Seed URL to start crawling from.

        Returns:
            List of discovered page URLs.
        """
        ...


@runtime_checkable
class MagnetExtractor(Protocol):
    """Protocol for magnet URI extraction."""

    async def extract(self, page_url: str) -> list[str]:
        """Extract magnet URIs from a page.

        Args:
            page_url: URL of the page to extract from.

        Returns:
            List of magnet URIs found on the page.
        """
        ...


@runtime_checkable
class JackettSearcher(Protocol):
    """Protocol for searching torrent indexers via Jackett."""

    async def search(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Search torrent indexers and return result dicts.

        Each result dict contains at least:
            - title: str
            - magnet_uri: str | None
            - size: int  (bytes)
            - seeders: int

        Args:
            query: Search query string.
            limit: Maximum number of results.

        Returns:
            List of result dicts.
        """
        ...


@runtime_checkable
class FlareSolverSession(Protocol):
    """Protocol for Cloudflare-bypass HTTP sessions."""

    async def get_html(self, url: str, *, timeout: int = 60) -> str:
        """Fetch a page's HTML after solving Cloudflare challenges.

        Args:
            url: Target page URL.
            timeout: Max time to wait in seconds.

        Returns:
            Decoded HTML string.
        """
        ...
