"""Tests for FFmpegRemuxer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pixav.media_loader.remuxer import FFmpegRemuxer
from pixav.shared.exceptions import RemuxError


@pytest.fixture
def remuxer() -> FFmpegRemuxer:
    return FFmpegRemuxer(ffmpeg_bin="ffmpeg", timeout=5)


class TestFFmpegRemuxer:
    async def test_remux_success(self, remuxer: FFmpegRemuxer, tmp_path: Path) -> None:
        input_file = tmp_path / "input.mkv"
        input_file.write_text("fake video data")
        output_file = tmp_path / "output.mp4"

        # Mock the subprocess to simulate successful FFmpeg
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            # Also need the output file to exist after "remux"
            output_file.write_text("remuxed data")
            await remuxer.remux(str(input_file), str(output_file))

            # Verify subprocess was called with stream copy
            call_args = mock_exec.call_args
        args = call_args[0]
        assert "-c" in args
        assert "copy" in args

    async def test_remux_input_not_found(self, remuxer: FFmpegRemuxer, tmp_path: Path) -> None:
        with pytest.raises(RemuxError, match="input file not found"):
            await remuxer.remux("/nonexistent/file.mkv", str(tmp_path / "out.mp4"))

    async def test_remux_ffmpeg_fails(self, remuxer: FFmpegRemuxer, tmp_path: Path) -> None:
        input_file = tmp_path / "input.mkv"
        input_file.write_text("fake video data")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"FFmpeg error output")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RemuxError, match="FFmpeg failed"):
                await remuxer.remux(str(input_file), str(tmp_path / "out.mp4"))

    async def test_remux_timeout(self, remuxer: FFmpegRemuxer, tmp_path: Path) -> None:
        input_file = tmp_path / "input.mkv"
        input_file.write_text("fake video data")

        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = asyncio.TimeoutError()
        mock_proc.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RemuxError, match="timed out"):
                await remuxer.remux(str(input_file), str(tmp_path / "out.mp4"))

        mock_proc.kill.assert_called_once()

    async def test_remux_binary_not_found(self, remuxer: FFmpegRemuxer, tmp_path: Path) -> None:
        input_file = tmp_path / "input.mkv"
        input_file.write_text("fake video data")

        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("ffmpeg")):
            with pytest.raises(RemuxError, match="FFmpeg binary not found"):
                await remuxer.remux(str(input_file), str(tmp_path / "out.mp4"))


class TestMakeOutputPath:
    def test_changes_extension_to_mp4(self) -> None:
        result = FFmpegRemuxer.make_output_path("/downloads/video.mkv", "/output")
        assert result == "/output/video.mp4"

    def test_handles_nested_path(self) -> None:
        result = FFmpegRemuxer.make_output_path("/a/b/c/movie.avi", "/output/dir")
        assert result == "/output/dir/movie.mp4"
