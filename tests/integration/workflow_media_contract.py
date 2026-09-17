"""Synthetic FFmpeg contract, executable in the existing runtime image without pytest."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from pixav.media_loader.preparation import inspect_media, prepare_media
from pixav.media_loader.remuxer import FFmpegRemuxer
from pixav.shared.exceptions import MediaDependencyError, RemuxError


async def verify_media_contract(directory: Path) -> None:
    source = directory / "synthetic.mkv"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=128x96:r=25:d=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-map",
        "1:a",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        str(source),
    )
    assert await proc.wait() == 0
    before = await inspect_media(str(source))
    artifact = await prepare_media(str(source), str(directory / "prepared.mp4"), FFmpegRemuxer())
    assert len(artifact.facts.streams) == 3
    assert (await inspect_media(str(source))).sha256 == before.sha256
    remuxer = AsyncMock()
    direct = await prepare_media(artifact.path, str(directory / "unneeded.mp4"), remuxer)
    assert direct.path == artifact.path
    reuse = await prepare_media(str(source), artifact.path, remuxer)
    assert reuse.facts.sha256 == artifact.facts.sha256
    remuxer.remux.assert_not_awaited()
    try:
        await prepare_media(str(source), str(directory / "failed.mp4"), FFmpegRemuxer(ffmpeg_bin="missing-ffmpeg"))
    except MediaDependencyError:
        pass
    else:
        raise AssertionError("missing FFmpeg dependency was not rejected")
    assert (await inspect_media(str(source))).sha256 == before.sha256
    corrupt = directory / "corrupt.mp4"
    corrupt.write_bytes(b"not media")
    try:
        await inspect_media(str(corrupt))
    except RemuxError:
        pass
    else:
        raise AssertionError("corrupt media was accepted")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="pixav-media-contract-") as root:
        asyncio.run(verify_media_contract(Path(root)))
    print("BDD-025/026/027/028/029/030/035: synthetic FFmpeg contract passed")
