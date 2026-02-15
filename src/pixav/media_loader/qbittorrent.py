"""QBittorrent client implementation via Web API."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from pixav.shared.exceptions import DownloadError

logger = logging.getLogger(__name__)

# qBittorrent Web API docs:
# https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)


class QBitClient:
    """Torrent client implementation using qBittorrent Web API.

    Implements the ``TorrentClient`` protocol.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        download_dir: str = "/downloads",
        timeout: int = 30,
        poll_interval: int = 10,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._download_dir = download_dir
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._sid: str | None = None

    async def _login(self, client: httpx.AsyncClient) -> None:
        """Authenticate and store the session cookie."""
        resp = await client.post(
            f"{self._base_url}/api/v2/auth/login",
            data={"username": self._username, "password": self._password},
        )
        if resp.text.strip().upper() != "OK.":
            raise DownloadError(f"qBittorrent login failed: {resp.text[:200]}")
        self._sid = resp.cookies.get("SID")
        logger.info("qBittorrent login successful")

    def _cookies(self) -> dict[str, str]:
        if self._sid:
            return {"SID": self._sid}
        return {}

    async def health_check(self) -> str:
        """Verify qBittorrent API reachability and authentication.

        Returns:
            qBittorrent version string from ``/api/v2/app/version``.

        Raises:
            DownloadError: If endpoint is unreachable, not qBittorrent, or auth fails.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                await self._login(client)
                version_resp = await client.get(f"{self._base_url}/api/v2/app/version")
                if version_resp.status_code == 404:
                    raise DownloadError(
                        f"qBittorrent health check failed: {self._base_url} "
                        "does not expose /api/v2/app/version (404)"
                    )
                if version_resp.status_code in {401, 403}:
                    raise DownloadError("qBittorrent health check failed: unauthorized even after login")
                version_resp.raise_for_status()

                version = version_resp.text.strip()
                if not version or "<html" in version.lower():
                    raise DownloadError("qBittorrent health check failed: invalid version response body")

                logger.info("qBittorrent health check ok (version=%s)", version)
                return version
        except DownloadError:
            raise
        except httpx.HTTPError as exc:
            raise DownloadError(f"qBittorrent health check request failed: {exc}") from exc

    async def add_magnet(self, uri: str) -> str:
        """Add a magnet URI to qBittorrent and return the torrent hash.

        The hash is extracted from the magnet URI's ``btih`` value.
        """
        torrent_hash = _extract_hash(uri)
        if not torrent_hash:
            raise DownloadError(f"Cannot extract hash from magnet URI: {uri[:80]}")

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                await self._login(client)
                resp = await client.post(
                    f"{self._base_url}/api/v2/torrents/add",
                    data={
                        "urls": uri,
                        "savepath": self._download_dir,
                    },
                    cookies=self._cookies(),
                )
                if resp.status_code != 200 or "fails" in resp.text.lower():
                    raise DownloadError(f"qBittorrent add_magnet failed: {resp.text[:200]}")
        except httpx.HTTPError as exc:
            raise DownloadError(f"qBittorrent request failed: {exc}") from exc

        logger.info("added torrent %s", torrent_hash)
        return torrent_hash

    async def wait_complete(self, torrent_hash: str, timeout: int = 3600) -> str:
        """Poll qBittorrent until the torrent finishes downloading.

        Returns the path to the downloaded content directory/file.
        """
        elapsed = 0

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                await self._login(client)

                while elapsed < timeout:
                    resp = await client.get(
                        f"{self._base_url}/api/v2/torrents/info",
                        params={"hashes": torrent_hash},
                        cookies=self._cookies(),
                    )
                    resp.raise_for_status()
                    torrents = resp.json()

                    if not torrents:
                        raise DownloadError(f"torrent {torrent_hash} not found in qBittorrent")

                    info = torrents[0]
                    progress = info.get("progress", 0)
                    state = info.get("state", "unknown")

                    if progress >= 1.0:
                        content_path = info.get("content_path", "")
                        save_path = info.get("save_path", self._download_dir)
                        result = content_path or str(Path(save_path) / info.get("name", ""))
                        logger.info("torrent %s complete: %s", torrent_hash, result)
                        return result

                    if state in ("error", "missingFiles"):
                        raise DownloadError(f"torrent {torrent_hash} in error state: {state}")

                    logger.debug(
                        "torrent %s progress=%.1f%% state=%s",
                        torrent_hash,
                        progress * 100,
                        state,
                    )
                    await asyncio.sleep(self._poll_interval)
                    elapsed += self._poll_interval

        except httpx.HTTPError as exc:
            raise DownloadError(f"qBittorrent polling failed: {exc}") from exc

        raise DownloadError(f"torrent {torrent_hash} download timed out after {timeout}s")

    async def delete_torrent(self, torrent_hash: str, delete_files: bool = True) -> None:
        """Delete a torrent and optionally its files."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                await self._login(client)
                resp = await client.post(
                    f"{self._base_url}/api/v2/torrents/delete",
                    data={
                        "hashes": torrent_hash,
                        "deleteFiles": "true" if delete_files else "false",
                    },
                    cookies=self._cookies(),
                )
                if resp.status_code != 200:
                    raise DownloadError(f"qBittorrent delete failed: {resp.text[:200]}")
        except httpx.HTTPError as exc:
            raise DownloadError(f"qBittorrent request failed: {exc}") from exc

        logger.info("deleted torrent %s (files=%s)", torrent_hash, delete_files)


def _extract_hash(magnet_uri: str) -> str | None:
    """Extract the info hash from a magnet URI."""
    import re

    match = re.search(r"btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet_uri)
    if match:
        return match.group(1).lower()
    return None
