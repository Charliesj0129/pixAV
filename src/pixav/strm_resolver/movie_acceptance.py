"""Opt-in HTTP and VLC acceptance of the complete locally rebuilt movie."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path

import httpx


def _local_digest(local: Path) -> str:
    digest = hashlib.sha256()
    with local.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def range_acceptance(base: str, video_id: str, local: Path) -> dict:
    size = local.stat().st_size
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resolved = await client.get(f"{base}/resolve/{video_id}")
        resolved.raise_for_status()
        ranges = [
            ("bytes=0-65535", 0, min(65535, size - 1)),
            (f"bytes={size // 2}-{min(size // 2 + 65535, size - 1)}", size // 2, min(size // 2 + 65535, size - 1)),
            ("bytes=-65536", max(0, size - 65536), size - 1),
        ]
        with local.open("rb") as source:
            for header, start, end in ranges:
                response = await client.get(f"{base}/stream/{video_id}", headers={"Range": header})
                source.seek(start)
                if (
                    response.status_code != 206
                    or response.headers.get("content-range") != f"bytes {start}-{end}/{size}"
                    or response.content != source.read(end - start + 1)
                ):
                    raise ValueError("complete movie HTTP range mismatch")
        invalid = await client.get(f"{base}/stream/{video_id}", headers={"Range": f"bytes={size}-"})
        if invalid.status_code != 416 or invalid.headers.get("content-range") != f"bytes */{size}":
            raise ValueError("HTTP 416 contract failed")
        head = await client.head(f"{base}/local/{video_id}")
        if head.status_code != 200 or head.content or int(head.headers.get("content-length", -1)) != size:
            raise ValueError("HTTP HEAD contract failed")

        # Stream both inputs: a complete 4K movie must never be buffered in RAM.
        expected = await asyncio.to_thread(_local_digest, local)
        digest = hashlib.sha256()
        received = 0
        async with client.stream(
            "GET", f"{base}/stream/{video_id}", headers={"Accept-Encoding": "identity"}
        ) as response:
            if response.status_code != 200 or int(response.headers.get("content-length", -1)) != size:
                raise ValueError("HTTP complete GET contract failed")
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > size:
                    raise ValueError("HTTP complete GET size mismatch")
                digest.update(chunk)
        if received != size or digest.hexdigest() != expected:
            raise ValueError("HTTP complete GET hash/size mismatch")
    return {
        "range": "PASS",
        "suffix": "PASS",
        "416": "PASS",
        "head": "PASS",
        "get": "PASS",
        "sha256": expected,
        "size": size,
    }


async def vlc_acceptance(prefix: list[str], url: str, seeks: list[float], evidence: Path) -> dict:
    receipts = []
    for index, seek in enumerate(seeks):
        result = await asyncio.to_thread(
            subprocess.run,
            prefix
            + [
                "cvlc",
                "-I",
                "dummy",
                "-vvv",
                "--no-audio",
                "--vout",
                "dummy",
                "--no-video-title-show",
                "--play-and-exit",
                "--start-time",
                str(seek),
                "--run-time",
                "5",
                url,
            ],
            capture_output=True,
            timeout=90,
            check=False,
        )
        log = result.stdout + result.stderr
        path = evidence / f"vlc-seek-{index}.log"
        path.write_bytes(log)
        # A zero exit code alone is not evidence of decoding a video picture.
        if result.returncode or b"Received first picture" not in log:
            raise ValueError("VLC did not decode a picture at the required seek")
        receipts.append({"seconds": seek, "log_sha256": hashlib.sha256(log).hexdigest()})
    return {"vlc": "PASS", "seeks": receipts}
