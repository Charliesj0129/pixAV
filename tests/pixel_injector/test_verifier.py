"""Tests for GooglePhotosVerifier."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from pixav.pixel_injector.verifier import GooglePhotosVerifier
from pixav.shared.exceptions import VerificationError


class TestGooglePhotosVerifier:
    @pytest.fixture
    def mock_adb(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def verifier(self, mock_adb: AsyncMock) -> GooglePhotosVerifier:
        return GooglePhotosVerifier(adb=mock_adb, timeout=5)

    async def test_wait_for_share_url_found(self, verifier: GooglePhotosVerifier, mock_adb: AsyncMock) -> None:
        mock_adb.shell.return_value = "I/GooglePhotos: upload complete https://photos.app.goo.gl/AbCdEfGh123\n"

        url = await verifier.wait_for_share_url("container-1", timeout=10)
        assert url == "https://photos.app.goo.gl/AbCdEfGh123"

    async def test_wait_for_share_url_timeout(self, verifier: GooglePhotosVerifier, mock_adb: AsyncMock) -> None:
        mock_adb.shell.return_value = "no url here"

        with pytest.raises(VerificationError, match="not found"):
            await verifier.wait_for_share_url("container-1", timeout=0)

    async def test_wait_for_share_url_no_adb(self) -> None:
        verifier = GooglePhotosVerifier(adb=None)
        with pytest.raises(VerificationError, match="no ADB"):
            await verifier.wait_for_share_url("container-1")

    @respx.mock
    async def test_validate_share_url_valid(self, verifier: GooglePhotosVerifier) -> None:
        respx.head("https://photos.app.goo.gl/test123").mock(return_value=httpx.Response(200))
        assert await verifier.validate_share_url("https://photos.app.goo.gl/test123") is True

    @respx.mock
    async def test_validate_share_url_invalid(self, verifier: GooglePhotosVerifier) -> None:
        respx.head("https://photos.app.goo.gl/expired").mock(return_value=httpx.Response(404))
        assert await verifier.validate_share_url("https://photos.app.goo.gl/expired") is False

    @respx.mock
    async def test_validate_share_url_connection_error(self, verifier: GooglePhotosVerifier) -> None:
        respx.head("https://photos.app.goo.gl/bad").mock(side_effect=httpx.ConnectError("refused"))
        assert await verifier.validate_share_url("https://photos.app.goo.gl/bad") is False
