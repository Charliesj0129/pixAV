"""Opt-in HTTP and VLC acceptance of complete movies and of the stable playback URL."""

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


def _windows(size: int) -> list[tuple[str, int, int]]:
    middle, window = size // 2, 65536
    return [
        ("bytes=0-65535", 0, min(window - 1, size - 1)),
        (f"bytes={middle}-{min(middle + window - 1, size - 1)}", middle, min(middle + window - 1, size - 1)),
        ("bytes=-65536", max(0, size - window), size - 1),
    ]


async def _hash_complete_get(client: httpx.AsyncClient, url: str, size: int, captured: dict) -> str:
    """Stream the whole object once, hashing it and keeping the bytes at each window."""
    digest = hashlib.sha256()
    received = 0
    async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
        if response.status_code != 200 or int(response.headers.get("content-length", -1)) != size:
            raise ValueError(f"HTTP complete GET contract failed ({response.status_code})")
        async for chunk in response.aiter_bytes():
            offset = received
            received += len(chunk)
            if received > size:
                raise ValueError("HTTP complete GET size mismatch")
            digest.update(chunk)
            for (start, end), buffer in captured.items():
                low, high = max(start, offset), min(end + 1, received)
                if low < high:
                    buffer += chunk[low - offset : high - offset]
    if received != size:
        raise ValueError("HTTP complete GET hash/size mismatch")
    return digest.hexdigest()


async def stable_acceptance(
    base: str,
    video_id: str,
    expected_size: int,
    expected_sha256: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    bearer_token: str | None = None,
) -> dict:
    """Accept the stable URL against the READY record alone, with no local original.

    The complete GET is hashed while it streams, and the bytes at each range's
    offsets are kept from that same body, so every range is compared with bytes
    whose SHA-256 was just proven. A cold object answers 503; try again later.
    """
    url = f"{base}/stream/{video_id}"
    size = expected_size
    windows = _windows(size)
    captured = {(start, end): bytearray() for _, start, end in windows}
    headers = {"Authorization": "Bearer " + bearer_token} if bearer_token else {}
    async with httpx.AsyncClient(timeout=60, follow_redirects=False, transport=transport, headers=headers) as client:
        head = await client.head(url)
        if (
            head.status_code != 200
            or head.content
            or int(head.headers.get("content-length", -1)) != size
            or head.headers.get("accept-ranges") != "bytes"
        ):
            raise ValueError("HTTP HEAD contract failed")
        if await _hash_complete_get(client, url, size, captured) != expected_sha256:
            raise ValueError("HTTP complete GET hash/size mismatch")

        for header, start, end in windows:
            response = await client.get(url, headers={"Range": header})
            if (
                response.status_code != 206
                or response.headers.get("content-range") != f"bytes {start}-{end}/{size}"
                or response.content != bytes(captured[(start, end)])
            ):
                raise ValueError("stable URL HTTP range mismatch")
        invalid = await client.get(url, headers={"Range": f"bytes={size}-"})
        if invalid.status_code != 416 or invalid.headers.get("content-range") != f"bytes */{size}":
            raise ValueError("HTTP 416 contract failed")
    return {
        "head": "PASS",
        "get": "PASS",
        "range": "PASS",
        "suffix": "PASS",
        "416": "PASS",
        "sha256": expected_sha256,
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
