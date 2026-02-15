"""Tests for QBitClient."""

from __future__ import annotations

import httpx
import pytest
import respx

from pixav.media_loader.qbittorrent import QBitClient, _extract_hash
from pixav.shared.exceptions import DownloadError


@pytest.fixture
def client() -> QBitClient:
    return QBitClient(
        base_url="http://qbit:8080",
        username="admin",
        password="adminadmin",
        download_dir="/downloads",
        timeout=5,
        poll_interval=0,  # skip sleep in tests
    )


class TestExtractHash:
    def test_extracts_40char_hex(self) -> None:
        magnet = "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709&dn=Test"
        assert _extract_hash(magnet) == "da39a3ee5e6b4b0d3255bfef95601890afd80709"

    def test_extracts_base32(self) -> None:
        magnet = "magnet:?xt=urn:btih:3I42H3S6NNFQ2MSVX7XZKYAYSCX5QBYJ&dn=Test"
        result = _extract_hash(magnet)
        assert result is not None
        assert len(result) == 32

    def test_returns_none_for_invalid(self) -> None:
        assert _extract_hash("not-a-magnet") is None
        assert _extract_hash("magnet:?xt=urn:btih:") is None


class TestQBitClient:
    @respx.mock
    async def test_health_check_success(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Ok."))
        respx.get("http://qbit:8080/api/v2/app/version").mock(return_value=httpx.Response(200, text="5.0.5"))

        version = await client.health_check()

        assert version == "5.0.5"

    @respx.mock
    async def test_health_check_wrong_endpoint(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Ok."))
        respx.get("http://qbit:8080/api/v2/app/version").mock(return_value=httpx.Response(404, text="Not found"))

        with pytest.raises(DownloadError, match="does not expose /api/v2/app/version"):
            await client.health_check()

    @respx.mock
    async def test_health_check_auth_fails(self, client: QBitClient) -> None:
        respx.get("http://qbit:8080/api/v2/app/version").mock(return_value=httpx.Response(200, text="5.0.5"))
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Fails."))

        with pytest.raises(DownloadError, match="login failed"):
            await client.health_check()

    @respx.mock
    async def test_add_magnet_success(self, client: QBitClient) -> None:
        # Login
        login_route = respx.post("http://qbit:8080/api/v2/auth/login").mock(
            return_value=httpx.Response(
                200,
                text="Ok.",
                headers={"Set-Cookie": "SID=abc123; path=/"},
            )
        )
        # Add torrent
        add_route = respx.post("http://qbit:8080/api/v2/torrents/add").mock(
            return_value=httpx.Response(200, text="Ok.")
        )

        magnet = "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709&dn=Test"
        result = await client.add_magnet(magnet)

        assert result == "da39a3ee5e6b4b0d3255bfef95601890afd80709"
        assert login_route.called
        assert add_route.called

    @respx.mock
    async def test_add_magnet_login_fails(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Fails."))

        magnet = "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709&dn=Test"
        with pytest.raises(DownloadError, match="login failed"):
            await client.add_magnet(magnet)

    async def test_add_magnet_invalid_hash(self, client: QBitClient) -> None:
        with pytest.raises(DownloadError, match="Cannot extract hash"):
            await client.add_magnet("not-a-magnet-link")

    @respx.mock
    async def test_wait_complete_success(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Ok."))
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "hash": "abc123",
                        "progress": 1.0,
                        "state": "uploading",
                        "content_path": "/downloads/test_video",
                        "save_path": "/downloads",
                        "name": "test_video",
                    }
                ],
            )
        )

        result = await client.wait_complete("abc123", timeout=10)
        assert result == "/downloads/test_video"

    @respx.mock
    async def test_wait_complete_not_found(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Ok."))
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(return_value=httpx.Response(200, json=[]))

        with pytest.raises(DownloadError, match="not found"):
            await client.wait_complete("missing", timeout=10)

    @respx.mock
    async def test_wait_complete_error_state(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Ok."))
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "hash": "abc123",
                        "progress": 0.5,
                        "state": "error",
                    }
                ],
            )
        )

        with pytest.raises(DownloadError, match="error state"):
            await client.wait_complete("abc123", timeout=10)

    @respx.mock
    async def test_wait_complete_timeout(self, client: QBitClient) -> None:
        respx.post("http://qbit:8080/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Ok."))
        respx.get("http://qbit:8080/api/v2/torrents/info").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "hash": "abc123",
                        "progress": 0.5,
                        "state": "downloading",
                    }
                ],
            )
        )

        # poll_interval=0 so it loops fast; timeout=0 means immediate timeout
        with pytest.raises(DownloadError, match="timed out"):
            await client.wait_complete("abc123", timeout=0)
