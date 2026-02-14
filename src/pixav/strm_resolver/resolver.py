"""Google Photos CDN URL resolver."""

from __future__ import annotations

import logging
import re

import httpx

from pixav.shared.exceptions import ResolveError

logger = logging.getLogger(__name__)

# Google Photos share URL → direct CDN link extraction
# The lh3.googleusercontent.com pattern in page source
_CDN_PATTERN = re.compile(r"(https://lh3\.googleusercontent\.com/[^\s\"']+)")


class GooglePhotosResolver:
    """Resolves Google Photos share URLs to direct CDN streaming URLs.

    Strategy:
    1. Fetch the share URL HTML page.
    2. Extract the lh3.googleusercontent.com CDN URL from the page content.
    3. Append video download parameters.
    """

    def __init__(self, *, timeout: int = 15) -> None:
        self._timeout = timeout

    async def resolve(self, share_url: str) -> str:
        """Resolve a Google Photos share URL to a CDN streaming URL.

        Args:
            share_url: The Google Photos share URL.

        Returns:
            Direct CDN streaming URL.

        Raises:
            ResolveError: If resolution fails.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
            ) as client:
                resp = await client.get(share_url)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ResolveError(f"share URL returned {exc.response.status_code}: {share_url}") from exc
        except httpx.HTTPError as exc:
            raise ResolveError(f"failed to fetch share URL: {exc}") from exc

        # Extract CDN URL from page content
        match = _CDN_PATTERN.search(resp.text)
        if not match:
            raise ResolveError(f"no CDN URL found in share page: {share_url}")

        cdn_base = match.group(1)
        # Clean and append video streaming params
        cdn_url = cdn_base.split("=")[0] + "=dv"
        logger.info("resolved %s → %s", share_url, cdn_url)
        return cdn_url
