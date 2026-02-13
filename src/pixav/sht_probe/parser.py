"""HTML parsing and magnet URI extraction via BeautifulSoup."""

from __future__ import annotations

import logging
import re
from typing import cast

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Match magnet URIs embedded in href or JS strings
_MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+[^\s\"'<>]*")


class BeautifulSoupExtractor:
    """Extract magnet URIs from HTML using BeautifulSoup + regex fallback."""

    async def extract(self, html: str) -> list[str]:
        """Extract unique magnet URIs from an HTML string.

        Looks for:
        1. ``<a href="magnet:...">`` tags
        2. Regex match across entire HTML for JS-embedded magnets

        Args:
            html: Raw HTML string to parse.

        Returns:
            De-duplicated list of magnet URIs.
        """
        magnets: set[str] = set()

        # 1. BeautifulSoup: parse <a> tags with magnet hrefs
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all("a", href=True):
            href_raw = tag.get("href")
            if not isinstance(href_raw, str):
                continue
            href = cast(str, href_raw)
            if href.startswith("magnet:?"):
                magnets.add(href)

        # 2. Regex fallback: catch magnets embedded in JS or other contexts
        for match in _MAGNET_RE.finditer(html):
            magnets.add(match.group(0))

        logger.debug("extracted %d magnet(s)", len(magnets))
        return list(magnets)
