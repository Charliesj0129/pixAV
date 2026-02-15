"""STRM file generator for media player integration."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import aiofiles  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


async def generate_strm(
    video_id: str,
    video_code: str,
    title: str,
    resolver_base_url: str,
    output_dir: str,
) -> str:
    """Generate a STRM file pointing to the resolver stream URL.

    Args:
        video_id: Unique video identifier.
        video_code: Video code/number (e.g., "ABC-123").
        title: Video title.
        resolver_base_url: Base URL of the strm_resolver service.
        output_dir: Directory to save the STRM file.

    Returns:
        Absolute path to the generated STRM file.
    """
    safe_title = _sanitize_filename(title)
    safe_code = _sanitize_filename(video_code)

    # Format: "ABC-123 - Title.strm"
    filename = f"{safe_code} - {safe_title}.strm"
    file_path = Path(output_dir) / filename

    stream_url = f"{resolver_base_url.rstrip('/')}/stream/{video_id}"

    # Ensure directory exists
    os.makedirs(output_dir, exist_ok=True)

    async with aiofiles.open(file_path, "w") as f:
        await f.write(stream_url)

    logger.info("generated STRM file: %s -> %s", file_path, stream_url)
    return str(file_path.absolute())


def _sanitize_filename(name: str) -> str:
    """Remove illegal characters from filename."""
    name = "".join(c for c in name if c.isalnum() or c in " .-_()[]").strip()
    return name.replace("..", "")
