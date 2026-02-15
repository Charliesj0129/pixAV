"""Upload verification implementation for Google Photos."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Protocol

import httpx

from pixav.pixel_injector.session import RedroidSession
from pixav.shared.exceptions import VerificationError

logger = logging.getLogger(__name__)

# Pattern for Google Photos share URLs
_SHARE_URL_PATTERN = re.compile(r"https://photos\.app\.goo\.gl/\w+")

# Logcat tag for Google Photos activity
_PHOTOS_LOGCAT_FILTER = "GooglePhotos"


class GooglePhotosVerifier:
    """Google Photos-specific implementation of UploadVerifier protocol.

    Monitors container logcat for share URL emission, then validates
    the URL via HTTP HEAD request.
    """

    def __init__(
        self,
        *,
        adb: _AdbClient | None = None,
        timeout: int = 15,
    ) -> None:
        self._adb = adb
        self._http_timeout = timeout

    async def _ensure_connected(self, session: RedroidSession) -> None:
        adb = self._adb
        if adb is None:
            raise VerificationError("no ADB connection configured")
        await adb.connect(session.adb_host, session.adb_port)

    async def wait_for_share_url(self, session: RedroidSession, timeout: int = 300) -> str:
        """Wait for and extract the Google Photos share URL from logcat.

        Polls the container's logcat output looking for a share URL pattern.

        Args:
            session: Active Redroid session.
            timeout: Maximum seconds to wait.

        Returns:
            Share URL string.

        Raises:
            VerificationError: If share URL not found within timeout.
        """
        if self._adb is None:
            raise VerificationError("no ADB connection configured")

        await self._ensure_connected(session)

        elapsed = 0
        poll_interval = 5

        while elapsed < timeout:
            try:
                # Read recent logcat from the container
                output = await self._adb.shell(f"logcat -d -t 100 -s {_PHOTOS_LOGCAT_FILTER}")
                match = _SHARE_URL_PATTERN.search(output)
                if match:
                    url = match.group(0)
                    logger.info("found share URL in %s: %s", session.container_id[:12], url)
                    return url
            except Exception as exc:
                logger.debug("logcat poll error on %s: %s", session.container_id[:12], exc)

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        raise VerificationError(f"share URL not found in container {session.container_id[:12]} after {timeout}s")

    async def validate_share_url(self, share_url: str) -> bool:
        """Validate that a share URL is accessible via HTTP HEAD.

        Args:
            share_url: URL to validate.

        Returns:
            True if URL responds with 2xx/3xx, False otherwise.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self._http_timeout,
                follow_redirects=True,
            ) as client:
                resp = await client.head(share_url)
                is_valid = resp.status_code < 400
                logger.info(
                    "share URL validation: %s → %d (%s)",
                    share_url,
                    resp.status_code,
                    "valid" if is_valid else "invalid",
                )
                return is_valid
        except httpx.HTTPError as exc:
            logger.warning("share URL validation failed: %s → %s", share_url, exc)
            return False


class _AdbClient(Protocol):
    async def connect(self, host: str, port: int) -> None: ...

    async def shell(self, cmd: str) -> str: ...
