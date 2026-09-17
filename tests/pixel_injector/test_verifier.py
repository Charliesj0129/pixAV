"""Tests for GooglePhotosVerifier."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from pixav.pixel_injector.session import RedroidSession
from pixav.pixel_injector.verifier import GooglePhotosVerifier, extract_share_url
from pixav.shared.exceptions import VerificationError


def test_extract_share_url_accepts_hyphen_and_underscore_without_punctuation() -> None:
    assert extract_share_url("shared=https://photos.app.goo.gl/Ab-Cd_12), next") == (
        "https://photos.app.goo.gl/Ab-Cd_12"
    )


def test_extract_share_url_returns_none_when_absent() -> None:
    assert extract_share_url("upload complete but private") is None


class TestGooglePhotosVerifier:
    @pytest.fixture
    def mock_adb(self) -> AsyncMock:
        adb = AsyncMock()
        adb.connect.return_value = None
        return adb

    @pytest.fixture
    def verifier(self, mock_adb: AsyncMock) -> GooglePhotosVerifier:
        return GooglePhotosVerifier(adb=mock_adb, timeout=5)

    @pytest.fixture
    def session(self) -> RedroidSession:
        return RedroidSession(
            task_id="task-1",
            container_id="container-1",
            adb_host="127.0.0.1",
            adb_port=32768,
        )

    async def test_wait_for_share_url_found(
        self,
        verifier: GooglePhotosVerifier,
        mock_adb: AsyncMock,
        session: RedroidSession,
    ) -> None:
        mock_adb.shell.return_value = "I/GooglePhotos: upload complete https://photos.app.goo.gl/AbCdEfGh123\n"

        url = await verifier.wait_for_share_url(session, timeout=10)
        assert url == "https://photos.app.goo.gl/AbCdEfGh123"
        mock_adb.connect.assert_awaited_once_with("127.0.0.1", 32768)

    async def test_wait_for_share_url_timeout(
        self,
        verifier: GooglePhotosVerifier,
        mock_adb: AsyncMock,
        session: RedroidSession,
    ) -> None:
        mock_adb.shell.return_value = "no url here"

        with pytest.raises(VerificationError, match="not found"):
            await verifier.wait_for_share_url(session, timeout=0)

    async def test_wait_for_share_url_no_adb(self, session: RedroidSession) -> None:
        verifier = GooglePhotosVerifier(adb=None)
        with pytest.raises(VerificationError, match="no ADB"):
            await verifier.wait_for_share_url(session)

    @respx.mock
    async def test_validate_share_url_valid(self, verifier: GooglePhotosVerifier) -> None:
        respx.head("https://photos.app.goo.gl/test123").mock(return_value=httpx.Response(200))
        assert await verifier.validate_share_url("https://photos.app.goo.gl/test123") is True

    @respx.mock
    async def test_validate_share_url_invalid(self, verifier: GooglePhotosVerifier) -> None:
        respx.head("https://photos.app.goo.gl/expired").mock(return_value=httpx.Response(404))
        assert await verifier.validate_share_url("https://photos.app.goo.gl/expired") is False

    @respx.mock
    async def test_validate_share_url_connection_error(
        self,
        verifier: GooglePhotosVerifier,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret_url = "https://photos.app.goo.gl/synthetic-secret"
        respx.head(secret_url).mock(side_effect=httpx.ConnectError(f"refused {secret_url}"))
        with caplog.at_level("WARNING"):
            assert await verifier.validate_share_url(secret_url) is False
        assert "synthetic-secret" not in caplog.text
