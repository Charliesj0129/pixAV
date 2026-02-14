"""Tests for AdbConnection."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pixav.pixel_injector.adb import AdbConnection
from pixav.shared.exceptions import AdbError


@pytest.fixture
def adb() -> AdbConnection:
    return AdbConnection(adb_bin="adb", timeout=5)


class TestAdbConnection:
    async def test_connect_success(self, adb: AdbConnection) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"connected to 10.0.0.1:5555", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await adb.connect("10.0.0.1", 5555)

        assert adb._target == "10.0.0.1:5555"

    async def test_connect_failure(self, adb: AdbConnection) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"cannot connect to 10.0.0.1:5555", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(AdbError, match="connect failed"):
                await adb.connect("10.0.0.1", 5555)

    async def test_push_success(self, adb: AdbConnection) -> None:
        adb._target = "10.0.0.1:5555"

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"1 file pushed", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await adb.push("/tmp/video.mp4", "/sdcard/DCIM/video.mp4")

    async def test_push_not_connected(self, adb: AdbConnection) -> None:
        with pytest.raises(AdbError, match="not connected"):
            await adb.push("/tmp/video.mp4", "/sdcard/video.mp4")

    async def test_push_failure(self, adb: AdbConnection) -> None:
        adb._target = "10.0.0.1:5555"

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"error: device not found")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(AdbError, match="push failed"):
                await adb.push("/tmp/video.mp4", "/sdcard/video.mp4")

    async def test_shell_success(self, adb: AdbConnection) -> None:
        adb._target = "10.0.0.1:5555"

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"output line", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await adb.shell("ls /sdcard")

        assert result == "output line"

    async def test_shell_not_connected(self, adb: AdbConnection) -> None:
        with pytest.raises(AdbError, match="not connected"):
            await adb.shell("ls")

    async def test_binary_not_found(self, adb: AdbConnection) -> None:
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("adb")):
            with pytest.raises(AdbError, match="not found"):
                await adb.connect("10.0.0.1", 5555)
