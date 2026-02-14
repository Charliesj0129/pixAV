"""Tests for UIAutomatorUploader."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pixav.pixel_injector.uploader import UIAutomatorUploader
from pixav.shared.exceptions import UploadError


@pytest.fixture
def mock_adb() -> AsyncMock:
    adb = AsyncMock()
    adb.push.return_value = None
    adb.shell.return_value = "Broadcasting: Intent..."
    return adb


@pytest.fixture
def uploader(mock_adb: AsyncMock) -> UIAutomatorUploader:
    return UIAutomatorUploader(adb=mock_adb)


class TestUIAutomatorUploader:
    async def test_push_file_success(self, uploader: UIAutomatorUploader, mock_adb: AsyncMock) -> None:
        result = await uploader.push_file("container-123", "/data/remuxed/video.mp4")

        assert result == "/sdcard/DCIM/Camera/video.mp4"
        mock_adb.push.assert_awaited_once_with("/data/remuxed/video.mp4", "/sdcard/DCIM/Camera/video.mp4")

    async def test_push_file_failure(self, uploader: UIAutomatorUploader, mock_adb: AsyncMock) -> None:
        mock_adb.push.side_effect = Exception("adb error")

        with pytest.raises(UploadError, match="failed to push"):
            await uploader.push_file("container-123", "/data/video.mp4")

    async def test_trigger_upload(self, uploader: UIAutomatorUploader, mock_adb: AsyncMock) -> None:
        await uploader.trigger_upload("container-123", "/sdcard/DCIM/Camera/video.mp4")

        mock_adb.shell.assert_awaited_once()
        cmd = mock_adb.shell.call_args[0][0]
        assert "MEDIA_SCANNER_SCAN_FILE" in cmd
        assert "/sdcard/DCIM/Camera/video.mp4" in cmd

    async def test_trigger_upload_failure(self, uploader: UIAutomatorUploader, mock_adb: AsyncMock) -> None:
        mock_adb.shell.side_effect = Exception("shell error")

        with pytest.raises(UploadError, match="failed to trigger"):
            await uploader.trigger_upload("container-123", "/sdcard/video.mp4")
