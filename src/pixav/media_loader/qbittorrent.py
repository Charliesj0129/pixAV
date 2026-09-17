"""QBittorrent client implementation via Web API."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple
from urllib.parse import quote, urlparse

import httpx

from pixav.shared.exceptions import DownloadError, SourceUnavailableError, TorrentOwnershipError
from pixav.shared.watermark import is_watermark_info_hash

logger = logging.getLogger(__name__)

# qBittorrent Web API docs:
# https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)

# States qBittorrent reports for a torrent that is genuinely making progress.
_ACTIVE_DOWNLOAD_STATES = frozenset({"downloading", "forcedDL", "checkingDL", "checkingResumeData", "moving"})
_TERMINAL_ERROR_STATES = frozenset({"error", "missingFiles"})


class MetadataProbe(NamedTuple):
    """Outcome of a metadata-only torrent lookup.

    ``info_hash`` is the hash this client actually handed to qBittorrent, which
    is derived from the magnet and is not necessarily the hash a caller holds in
    its own records. Cleaning up by any other value silently leaks the torrent.
    """

    name: str | None
    created: bool
    info_hash: str


def classify_torrent_progress(info: Mapping[str, Any]) -> str:
    """Classify one ``/torrents/info`` row as complete/error/viable/waiting/stalled.

    ``num_seeds`` is deliberately not consulted: it counts *connected* peers, so
    it reads 0 on every healthy torrent that has not finished its handshake yet.
    Measured on 2026-08-30, torrents whose tracker reported ``num_complete=2``
    still showed ``num_seeds=0``. The swarm-existence signals are
    ``num_complete`` (seeds the tracker knows of) and ``availability``
    (distributed copies).
    """
    progress = float(info.get("progress") or 0.0)
    state = str(info.get("state") or "unknown")

    if progress >= 1.0:
        return "complete"
    if state in _TERMINAL_ERROR_STATES:
        return "error"
    if progress > 0.0 or state in _ACTIVE_DOWNLOAD_STATES:
        return "viable"
    if int(info.get("num_complete") or 0) > 0 or float(info.get("availability") or 0.0) > 0.0:
        # A swarm exists; we just have not connected to it yet. Bounded by the
        # overall download timeout, not by the no-peer grace period.
        return "waiting"
    return "stalled"


def reject_watermark_hash(info_hash: str) -> None:
    """Refuse a 40-hex string that is an XOR-obfuscated watermark, not a torrent.

    Sehuatang publishes these alongside real magnets. They can never resolve
    metadata, so qBittorrent parks them in ``metaDL`` forever, holding an active
    download slot against ``max_active_downloads``. Three of them deadlocked the
    production client on 2026-08-30.
    """
    if is_watermark_info_hash(info_hash):
        raise SourceUnavailableError(f"magnet info hash is a Sehuatang watermark, not a torrent: {info_hash}")


def parse_extra_trackers(raw: str) -> tuple[str, ...]:
    """Parse and validate an operator-maintained tracker list."""
    trackers: list[str] = []
    for item in re.split(r"[\r\n,]+", raw):
        tracker = item.strip()
        if not tracker or tracker in trackers:
            continue
        if urlparse(tracker).scheme.lower() not in {"http", "https", "udp"}:
            logger.warning("ignoring unsupported tracker URL: %s", tracker[:100])
            continue
        trackers.append(tracker)
    return tuple(trackers)


def map_download_path(remote_path: str, *, remote_root: str, local_root: str) -> str:
    """Translate a qBittorrent-container path into the worker namespace.

    Both containers bind the same host directory at different mount points.
    Prefix mapping is explicit and rejects traversal instead of performing a
    string replacement on a path supplied by an external service.
    """
    remote_base = PurePosixPath(remote_root)
    candidate = PurePosixPath(remote_path)
    if not remote_base.is_absolute() or not candidate.is_absolute():
        raise DownloadError("qBittorrent download paths must be absolute")
    try:
        relative = candidate.relative_to(remote_base)
    except ValueError as exc:
        raise DownloadError(f"qBittorrent returned path outside configured download root: {remote_path}") from exc
    if not relative.parts:
        raise DownloadError("qBittorrent returned the download root without a content path")
    if ".." in relative.parts:
        raise DownloadError("qBittorrent returned path traversal")

    local_base = Path(local_root).expanduser().resolve(strict=False)
    untranslated = local_base / Path(*relative.parts)
    if untranslated.is_symlink() or any(parent.is_symlink() for parent in untranslated.parents):
        raise DownloadError("symlink in qBittorrent artifact path")
    translated = untranslated.resolve(strict=False)
    try:
        translated.relative_to(local_base)
    except ValueError as exc:  # pragma: no cover - PurePosixPath blocks ordinary traversal
        raise DownloadError(f"translated download path escapes local root: {remote_path}") from exc
    return os.fspath(translated)


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
        local_download_dir: str | None = None,
        timeout: int = 30,
        poll_interval: int = 10,
        extra_trackers: tuple[str, ...] = (),
        download_timeout: int = 3600,
        no_peer_grace_seconds: int = 300,
        max_download_bytes: int | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._download_dir = download_dir
        self._local_download_dir = local_download_dir
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._extra_trackers = extra_trackers
        self._download_timeout = download_timeout
        self._no_peer_grace_seconds = no_peer_grace_seconds
        self._max_download_bytes = max_download_bytes
        self._session_cookies: dict[str, str] = {}
        # One worker owns one client for its lifetime. Reusing it preserves the
        # qBittorrent session cookie and the underlying connection pool instead
        # of logging in and opening a new TCP connection for every adapter call.
        self._client: httpx.AsyncClient | None = None
        self._login_lock = asyncio.Lock()
        self._authenticated = False

    def _http_client(self) -> httpx.AsyncClient:
        """Return the long-lived HTTP client, creating it lazily."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def _authenticated_client(self) -> httpx.AsyncClient:
        """Return the persistent client after performing at most one login."""
        client = self._http_client()
        if self._authenticated:
            return client
        async with self._login_lock:
            if not self._authenticated:
                await self._login(client)
                self._authenticated = True
        return client

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Issue one authenticated request, refreshing an expired session once."""
        client = await self._authenticated_client()
        url = f"{self._base_url}{path}"
        response = await client.request(method, url, **kwargs)
        if response.status_code not in {401, 403}:
            return response

        # qBittorrent invalidates its cookie on restart. A persistent adapter
        # must recover without requiring a worker restart, but retries login at
        # most once so bad credentials/IP bans still fail loudly.
        async with self._login_lock:
            self._authenticated = False
            client.cookies.clear()
            await self._login(client)
            self._authenticated = True
        return await client.request(method, url, **kwargs)

    async def aclose(self) -> None:
        """Close the persistent connection pool owned by this adapter."""
        client = self._client
        self._client = None
        self._authenticated = False
        self._session_cookies = {}
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> QBitClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    def _with_trackers(self, uri: str) -> str:
        """Put the fallback trackers in the magnet itself, not just after the add.

        Adding them afterwards leaves the client announcing to nothing until its
        next announce cycle, which can outlast the no-peer grace period and get a
        perfectly live swarm classified as dead. A magnet that already carries
        ``tr`` parameters is left untouched.
        """
        if not self._extra_trackers or "&tr=" in uri or "?tr=" in uri:
            return uri
        return uri + "".join("&tr=" + quote(tracker, safe="") for tracker in self._extra_trackers)

    async def _add_extra_trackers(self, torrent_hash: str) -> None:
        """Best-effort tracker fallback; a tracker outage must not reject a magnet."""
        if not self._extra_trackers:
            return
        try:
            response = await self._request(
                "POST",
                "/api/v2/torrents/addTrackers",
                data={"hash": torrent_hash, "urls": "\n".join(self._extra_trackers)},
            )
            if response.status_code != 200:
                logger.warning(
                    "qBittorrent rejected fallback trackers for %s: HTTP %d %s",
                    torrent_hash,
                    response.status_code,
                    response.text[:200],
                )
        except httpx.HTTPError as exc:
            logger.warning("failed to add fallback trackers for %s: %s", torrent_hash, exc)

    async def _login(self, client: httpx.AsyncClient) -> None:
        """Authenticate and store the session cookie.

        The login contract differs across qBittorrent releases, so this checks
        the status code rather than a specific body or cookie name:

        * <= 5.1 answers ``200`` with the body ``Ok.`` and a ``SID`` cookie, and
          reports bad credentials as ``200`` with the body ``Fails.``
        * >= 5.2 answers ``204`` with an empty body and a ``QBT_SID_<port>``
          cookie, and reports bad credentials as ``401``
        """
        resp = await client.post(
            f"{self._base_url}/api/v2/auth/login",
            data={"username": self._username, "password": self._password},
        )
        body = resp.text.strip()
        if resp.status_code >= 400 or body.upper() == "FAILS.":
            detail = body[:200] or f"HTTP {resp.status_code}"
            raise DownloadError(f"qBittorrent login failed: {detail}")

        # Keep every cookie the server set: the session cookie is named SID on
        # older builds and QBT_SID_<port> on newer ones.
        self._session_cookies = dict(resp.cookies)
        if not self._session_cookies:
            raise DownloadError("qBittorrent login failed: server returned no session cookie")
        client.cookies.update(self._session_cookies)
        logger.info("qBittorrent login successful")

    async def health_check(self) -> str:
        """Verify qBittorrent API reachability and authentication.

        Returns:
            qBittorrent version string from ``/api/v2/app/version``.

        Raises:
            DownloadError: If endpoint is unreachable, not qBittorrent, or auth fails.
        """
        try:
            version_resp = await self._request("GET", "/api/v2/app/version")
            if version_resp.status_code == 404:
                raise DownloadError(
                    f"qBittorrent health check failed: {self._base_url} does not expose /api/v2/app/version (404)"
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

    async def reconcile_download(self, info_hash: str, operation_id: str) -> str:
        """Reuse only a torrent bearing this operation's tag, without deleting files."""
        from uuid import UUID

        if not re.fullmatch(r"[a-f0-9]{40}", info_hash):
            raise DownloadError("invalid torrent identity")
        tag = f"pixav-operation-{UUID(operation_id)}"
        response = await self._request("GET", "/api/v2/torrents/info", params={"hashes": info_hash})
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or len(rows) > 1:
            raise DownloadError("invalid torrent reconciliation response")
        if rows:
            row = rows[0]
            if (
                not isinstance(row, dict)
                or row.get("hash") != info_hash
                or tag not in str(row.get("tags", "")).split(", ")
            ):
                raise TorrentOwnershipError("torrent ownership is unknown")
        else:
            reject_watermark_hash(info_hash)
            response = await self._request(
                "POST",
                "/api/v2/torrents/add",
                data={
                    "urls": self._with_trackers(f"magnet:?xt=urn:btih:{info_hash}"),
                    "savepath": self._download_dir,
                    "tags": tag,
                },
            )
            response.raise_for_status()
            # An acknowledged add is still reconciled before reading any artifact.
            response = await self._request("GET", "/api/v2/torrents/info", params={"hashes": info_hash})
            response.raise_for_status()
            rows = response.json()
            if (
                not isinstance(rows, list)
                or len(rows) != 1
                or not isinstance(rows[0], dict)
                or rows[0].get("hash") != info_hash
                or tag not in str(rows[0].get("tags", "")).split(", ")
            ):
                raise TorrentOwnershipError("torrent add result requires reconciliation")
        return await self.wait_complete(info_hash)

    async def add_magnet(self, uri: str) -> str:
        """Add a magnet URI to qBittorrent and return the torrent hash.

        The hash is extracted from the magnet URI's ``btih`` value.
        """
        torrent_hash = extract_info_hash(uri)
        if not torrent_hash:
            raise DownloadError(f"Cannot extract hash from magnet URI: {uri[:80]}")
        reject_watermark_hash(torrent_hash)

        try:
            resp = await self._request(
                "POST",
                "/api/v2/torrents/add",
                data={
                    "urls": self._with_trackers(uri),
                    "savepath": self._download_dir,
                },
            )
            if resp.status_code != 200 or "fails" in resp.text.lower():
                # Check if torrent already exists to make addition idempotent
                check_resp = await self._request(
                    "GET",
                    "/api/v2/torrents/info",
                    params={"hashes": torrent_hash},
                )
                if check_resp.status_code == 200 and check_resp.json():
                    logger.info("torrent %s already exists in qBittorrent", torrent_hash)
                    await self._add_extra_trackers(torrent_hash)
                    return torrent_hash
                raise DownloadError(f"qBittorrent add_magnet failed: {resp.text[:200]}")
            await self._add_extra_trackers(torrent_hash)
        except httpx.HTTPError as exc:
            raise DownloadError(f"qBittorrent request failed: {exc}") from exc

        logger.info("added torrent %s", torrent_hash)
        return torrent_hash

    async def add_torrent_file(self, content: bytes, expected_hash: str) -> str:
        """Add a ``.torrent`` file and return its hash.

        Preferred over :meth:`add_magnet` wherever the source publishes one,
        because the file carries the uploader's trackers. Bare magnets on this
        site carry none, leaving a client with only DHT to find a swarm.

        The caller states the hash it believes it is adding; a mismatch means
        the attachment is not the release that was selected, so the torrent is
        removed again rather than silently downloaded.
        """
        torrent_hash = expected_hash.lower()
        if not re.fullmatch(r"[a-f0-9]{40}", torrent_hash):
            raise DownloadError("expected torrent hash is not a 40-hex info hash")
        reject_watermark_hash(torrent_hash)

        try:
            before = await self.list_torrent_hashes()
            resp = await self._request(
                "POST",
                "/api/v2/torrents/add",
                files={"torrents": ("source.torrent", content, "application/x-bittorrent")},
                data={"savepath": self._download_dir},
            )
            if resp.status_code != 200 or "fails" in resp.text.lower():
                raise DownloadError(f"qBittorrent add_torrent_file failed: {resp.text[:200]}")
            if torrent_hash not in await self.list_torrent_hashes():
                # Remove whatever the attachment actually was, so a wrong file
                # never occupies a download slot or reaches the disk.
                for stray in await self.list_torrent_hashes() - before:
                    await self._discard_metadata_torrent(stray)
                raise DownloadError("torrent file does not match the selected info hash")
            await self._add_extra_trackers(torrent_hash)
        except httpx.HTTPError as exc:
            raise DownloadError(f"qBittorrent request failed: {exc}") from exc

        logger.info("added torrent %s from attached file", torrent_hash)
        return torrent_hash

    async def _enforce_size_limit(self, info: Mapping[str, Any], torrent_hash: str) -> None:
        if (
            self._max_download_bytes is not None
            and int(info.get("total_size") or info.get("size") or 0) > self._max_download_bytes
        ):
            stopped = await self._request("POST", "/api/v2/torrents/stop", data={"hashes": torrent_hash})
            if stopped.status_code == 404:
                stopped = await self._request("POST", "/api/v2/torrents/pause", data={"hashes": torrent_hash})
            stopped.raise_for_status()
            raise DownloadError("torrent exceeds reserved disk budget; stopped without deleting files")

    async def wait_complete(self, torrent_hash: str, timeout: int | None = None) -> str:
        """Poll qBittorrent until the torrent finishes downloading.

        Returns the path to the downloaded content directory/file.

        Raises:
            DownloadError: A no-peer observation is a temporary failure, never
                independent evidence that a source is unavailable.
            DownloadError: The torrent client errored, or the overall timeout
                elapsed while the torrent was still viable.
        """
        deadline = self._download_timeout if timeout is None else timeout
        elapsed = 0
        stalled_for = 0

        try:
            while elapsed < deadline:
                resp = await self._request(
                    "GET",
                    "/api/v2/torrents/info",
                    params={"hashes": torrent_hash},
                )
                resp.raise_for_status()
                torrents = resp.json()

                if not torrents:
                    raise DownloadError(f"torrent {torrent_hash} not found in qBittorrent")

                info = torrents[0]
                await self._enforce_size_limit(info, torrent_hash)
                verdict = classify_torrent_progress(info)

                if verdict == "complete":
                    content_path = info.get("content_path", "")
                    save_path = info.get("save_path", self._download_dir)
                    result = content_path or str(PurePosixPath(save_path) / info.get("name", ""))
                    if self._local_download_dir is not None:
                        result = map_download_path(
                            result,
                            remote_root=self._download_dir,
                            local_root=self._local_download_dir,
                        )
                    logger.info("torrent %s complete", torrent_hash)
                    return result

                if verdict == "error":
                    raise DownloadError(f"torrent {torrent_hash} in error state: {info.get('state')}")

                if verdict == "stalled":
                    stalled_for += self._poll_interval
                    if stalled_for >= self._no_peer_grace_seconds:
                        raise DownloadError(
                            f"torrent {torrent_hash} found no seeds within "
                            f"{self._no_peer_grace_seconds}s (state={info.get('state')}, "
                            f"num_complete={info.get('num_complete')}, "
                            f"availability={info.get('availability')})"
                        )
                else:
                    stalled_for = 0

                logger.debug(
                    "torrent %s progress=%.1f%% state=%s verdict=%s num_complete=%s",
                    torrent_hash,
                    float(info.get("progress") or 0.0) * 100,
                    info.get("state"),
                    verdict,
                    info.get("num_complete"),
                )
                await asyncio.sleep(self._poll_interval)
                elapsed += self._poll_interval

        except httpx.HTTPError as exc:
            raise DownloadError(f"qBittorrent polling failed: {exc}") from exc

        raise DownloadError(f"torrent {torrent_hash} download timed out after {deadline}s")

    async def delete_torrent(self, torrent_hash: str, delete_files: bool = True) -> None:
        """Delete a torrent and optionally its files."""
        try:
            resp = await self._request(
                "POST",
                "/api/v2/torrents/delete",
                data={
                    "hashes": torrent_hash,
                    "deleteFiles": "true" if delete_files else "false",
                },
            )
            if resp.status_code != 200:
                raise DownloadError(f"qBittorrent delete failed: {resp.text[:200]}")
        except httpx.HTTPError as exc:
            raise DownloadError(f"qBittorrent request failed: {exc}") from exc

        logger.info("deleted torrent %s (files=%s)", torrent_hash, delete_files)

    async def list_torrent_hashes(self) -> set[str]:
        """Read only inventory for reconciliation; never expose names or tracker URLs."""
        response = await self._request("GET", "/api/v2/torrents/info")
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not re.fullmatch(r"[a-fA-F0-9]{40}", str(row.get("hash", ""))) for row in rows
        ):
            raise DownloadError("qBittorrent inventory contract failure")
        return {str(row["hash"]).lower() for row in rows}

    async def has_torrent(self, torrent_hash: str) -> bool:
        """Return whether qBittorrent still owns ``torrent_hash``.

        This is deliberately a read-only lifecycle assertion for live gates;
        callers must not infer successful cleanup merely from the delete POST.
        """
        try:
            response = await self._request(
                "GET",
                "/api/v2/torrents/info",
                params={"hashes": torrent_hash},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DownloadError(f"qBittorrent cleanup verification failed: {exc}") from exc
        if not isinstance(payload, list):
            raise DownloadError("qBittorrent cleanup verification returned a non-list payload")
        expected = torrent_hash.casefold()
        return any(isinstance(item, Mapping) and str(item.get("hash") or "").casefold() == expected for item in payload)

    async def fetch_metadata_name(self, magnet_uri: str, *, timeout: int = 120) -> MetadataProbe:
        """Fetch a torrent name without starting content download.

        Returns a :class:`MetadataProbe` carrying the name, whether this call
        created the torrent, and **the hash this client actually added**.
        Existing torrents are never removed by this operation; callers may clean
        up only when ``created`` is true, and must clean up by the returned
        ``info_hash``.

        If this call creates a torrent and then fails, it removes that torrent
        before raising. Otherwise the failure path leaks a metadata-only torrent
        that occupies an active download slot forever, which is exactly how three
        of them deadlocked the production client.
        """
        torrent_hash = extract_info_hash(magnet_uri)
        if not torrent_hash:
            raise DownloadError("cannot extract torrent hash for metadata lookup")
        reject_watermark_hash(torrent_hash)
        created = False
        try:
            response = await self._request("GET", "/api/v2/torrents/info", params={"hashes": torrent_hash})
            response.raise_for_status()
            if not response.json():
                await self._add_metadata_only(magnet_uri, torrent_hash)
                created = True

            name = await self._poll_for_name(torrent_hash, timeout=timeout)
            return MetadataProbe(name, created, torrent_hash)
        except httpx.HTTPError as exc:
            if created:
                await self._discard_metadata_torrent(torrent_hash)
            raise DownloadError(f"qBittorrent metadata lookup failed: {exc}") from exc
        except BaseException:
            if created:
                await self._discard_metadata_torrent(torrent_hash)
            raise

    async def _add_metadata_only(self, magnet_uri: str, torrent_hash: str) -> None:
        """Add a stopped torrent purely to resolve its metadata."""
        add = await self._request(
            "POST",
            "/api/v2/torrents/add",
            data={"urls": magnet_uri, "savepath": self._download_dir, "stopped": "true"},
        )
        if add.status_code != 200 or "fails" in add.text.lower():
            raise DownloadError(f"qBittorrent metadata-only add failed: {add.text[:200]}")
        await self._add_extra_trackers(torrent_hash)

    async def _poll_for_name(
        self,
        torrent_hash: str,
        *,
        timeout: int,
    ) -> str | None:
        """Wait for qBittorrent to resolve a real name, not an echo of the hash."""
        elapsed = 0
        while elapsed < timeout:
            response = await self._request("GET", "/api/v2/torrents/info", params={"hashes": torrent_hash})
            response.raise_for_status()
            torrents = response.json()
            if torrents:
                name = str(torrents[0].get("name") or "").strip()
                if name and name.casefold() != torrent_hash.casefold():
                    return name
            await asyncio.sleep(self._poll_interval)
            elapsed += self._poll_interval
        return None

    async def _discard_metadata_torrent(self, torrent_hash: str) -> None:
        """Best-effort removal of a torrent this client created and then abandoned."""
        try:
            await self.delete_torrent(torrent_hash, delete_files=False)
        except (DownloadError, httpx.HTTPError) as exc:
            logger.warning("failed to discard metadata torrent %s: %s", torrent_hash, exc)


def extract_info_hash(magnet_uri: str) -> str | None:
    """Extract the info hash from a magnet URI."""
    match = re.search(r"btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet_uri)
    if match:
        return match.group(1).lower()
    return None
