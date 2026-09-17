"""FFmpeg-based media remuxing."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from pixav.shared.exceptions import MediaDependencyError, RemuxError

logger = logging.getLogger(__name__)

_MEDIA_EXTENSIONS = frozenset({".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm"})
_SAMPLE_TOKENS = frozenset({"sample", "trailer"})


def _has_symlink(path: Path) -> bool:
    return any(part.is_symlink() for part in (path, *path.parents))


def _download_root(download_path: str) -> Path:
    original = Path(download_path).expanduser()
    if _has_symlink(original):
        raise RemuxError("symlink in downloaded media path")
    try:
        return original.resolve(strict=True)
    except OSError as exc:
        raise RemuxError(f"download path not found: {download_path}") from exc


def select_media_input(download_path: str) -> str:
    """Select the largest supported file, excluding samples and symlinks."""
    root = _download_root(download_path)
    if root.is_file():
        return os.fspath(root)
    if not root.is_dir():
        raise RemuxError(f"download path is neither a file nor directory: {download_path}")

    candidates: list[tuple[int, str, Path]] = []
    for candidate in root.rglob("*"):
        if _has_symlink(candidate):
            continue
        if candidate.suffix.casefold() not in _MEDIA_EXTENSIONS:
            continue
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if not resolved.is_file():
            continue
        stem = resolved.stem.replace("-", " ").replace("_", " ")
        tokens = {part.casefold() for part in stem.split()}
        if tokens & _SAMPLE_TOKENS:
            continue
        candidates.append((resolved.stat().st_size, os.fspath(resolved), resolved))

    if not candidates:
        raise RemuxError(f"no supported media file found under: {download_path}")
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return os.fspath(candidates[0][2])


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
            "-map",
            "0",  # preserve every stream
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
            await proc.wait()
            raise MediaDependencyError(f"FFmpeg timed out after {self._timeout}s") from exc
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        except FileNotFoundError as exc:
            raise MediaDependencyError(f"FFmpeg binary not found: {self._ffmpeg_bin}") from exc

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace")[-500:] if stderr else "unknown error"
            raise MediaDependencyError(f"FFmpeg failed (rc={proc.returncode}): {err_msg}")

        # Verify output was created
        if not os.path.isfile(output_path):
            raise RemuxError(f"FFmpeg produced no output file: {output_path}")

        out_size = os.path.getsize(output_path)
        logger.info("remux complete: %s (%.1f MB)", output_path, out_size / 1_048_576)

    @staticmethod
    def make_output_path(input_path: str, output_dir: str, *, unique_key: str | None = None) -> str:
        """Generate the output path by changing extension to .mp4.

        Args:
            input_path: Original file path.
            output_dir: Directory for remuxed output.

        Returns:
            Output file path with .mp4 extension.
        """
        stem = Path(input_path).stem
        parent = Path(output_dir) / unique_key if unique_key else Path(output_dir)
        output = parent / f"{stem}.mp4"
        if output.resolve(strict=False) == Path(input_path).resolve(strict=False):
            output = parent / f"{stem}.remuxed.mp4"
        return str(output)
