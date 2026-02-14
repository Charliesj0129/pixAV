"""FFmpeg-based media remuxing."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from pixav.shared.exceptions import RemuxError

logger = logging.getLogger(__name__)


class FFmpegRemuxer:
    """Media remuxer implementation using FFmpeg subprocess.

    Implements the ``Remuxer`` protocol.
    Remuxes media files to MP4 container without re-encoding
    (stream copy) for maximum speed.
    """

    def __init__(self, *, ffmpeg_bin: str = "ffmpeg", timeout: int = 600) -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._timeout = timeout

    async def remux(self, input_path: str, output_path: str) -> None:
        """Remux media from input to output using FFmpeg stream copy.

        Args:
            input_path: Path to source media file.
            output_path: Path to write remuxed MP4 output.

        Raises:
            RemuxError: If input doesn't exist, FFmpeg fails, or times out.
        """
        if not os.path.isfile(input_path):
            raise RemuxError(f"input file not found: {input_path}")

        # Ensure output directory exists
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self._ffmpeg_bin,
            "-y",  # overwrite output
            "-i",
            input_path,
            "-c",
            "copy",  # stream copy — no re-encoding
            "-movflags",
            "+faststart",  # web-friendly MP4
            output_path,
        ]

        logger.info("remuxing %s → %s", input_path, output_path)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise RemuxError(f"FFmpeg timed out after {self._timeout}s") from exc
        except FileNotFoundError as exc:
            raise RemuxError(f"FFmpeg binary not found: {self._ffmpeg_bin}") from exc

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace")[-500:] if stderr else "unknown error"
            raise RemuxError(f"FFmpeg failed (rc={proc.returncode}): {err_msg}")

        # Verify output was created
        if not os.path.isfile(output_path):
            raise RemuxError(f"FFmpeg produced no output file: {output_path}")

        out_size = os.path.getsize(output_path)
        logger.info("remux complete: %s (%.1f MB)", output_path, out_size / 1_048_576)

    @staticmethod
    def make_output_path(input_path: str, output_dir: str) -> str:
        """Generate the output path by changing extension to .mp4.

        Args:
            input_path: Original file path.
            output_dir: Directory for remuxed output.

        Returns:
            Output file path with .mp4 extension.
        """
        stem = Path(input_path).stem
        return str(Path(output_dir) / f"{stem}.mp4")
