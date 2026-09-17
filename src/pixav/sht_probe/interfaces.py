"""Interfaces for SHT-Probe module."""

from __future__ import annotations

from typing import Protocol, TypedDict, runtime_checkable


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


class IndexerResult(TypedDict):
    """Normalized discovery candidate returned by any indexer adapter."""

    title: str
    magnet_uri: str | None
    source_url: str
    size: int
    seeders: int


@runtime_checkable
class IndexerAdapter(Protocol):
    """Domain boundary for Jackett/Cardigann or a retained source adapter."""

    async def search(self, query: str, *, limit: int = 50) -> list[IndexerResult]:
        """Search an indexer and return normalized candidates.

        Each result dict contains at least:
            - title: str
            - magnet_uri: str | None
            - source_url: str
            - size: int  (bytes)
            - seeders: int

        Args:
            query: Search query string.
            limit: Maximum number of results.

        Returns:
            List of result dicts.
        """
        ...


# Compatibility name for existing imports while callers migrate to the domain
# contract. This is one protocol, not a second execution path.
JackettSearcher = IndexerAdapter


@runtime_checkable
class FlareSolverSession(Protocol):
    """Protocol for Cloudflare-bypass HTTP sessions."""

    async def get_html(
        self,
        url: str,
        *,
        timeout: int = 60,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]] | tuple[str, dict[str, str], str]:
        """Fetch a page's HTML after solving Cloudflare challenges.

        Args:
            url: Target page URL.
            timeout: Max time to wait in seconds.
            cookies: Optional cookies to seed the session.
            headers: Optional request headers.

        Returns:
            Tuple of (html_string, cookies_dict) or
            (html_string, cookies_dict, user_agent_string).
        """
        ...
