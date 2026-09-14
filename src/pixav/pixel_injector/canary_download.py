"""Independent browser download and byte verification for one Photos canary.

Also executable in the isolated tools image; Playwright is imported only there.
No credentials, signed URLs or page text are emitted by this collector.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import IO, Any


class OriginalMismatchError(ValueError):
    """Downloaded bytes or media cannot be the requested original."""


def copy_original(stream: IO[bytes], destination: Path, size: int, digest: str) -> None:
    """Accept exactly the expected original bytes; never publish a partial result."""
    partial = destination.with_suffix(destination.suffix + ".part")
    if destination.exists() or destination.is_symlink():
        raise ValueError("original destination already exists")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    measured = hashlib.sha256()
    count = 0
    try:
        with os.fdopen(fd, "wb") as target:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                count += len(chunk)
                if count > size:
                    raise OriginalMismatchError("cloud file exceeds source size")
                target.write(chunk)
                measured.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        if count != size or measured.hexdigest() != digest:
            raise OriginalMismatchError("cloud original size or SHA-256 mismatch")
        os.link(partial, destination)  # Atomic publication without overwriting an existing file.
    finally:
        partial.unlink(missing_ok=True)


def extract_original(archive: Path, destination: Path, filename: str, size: int, digest: str) -> None:
    """Download-all must contain exactly one regular item with the expected name."""
    # DEFLATE can expand incompressible data by more than 1 MiB for a 9.5 GB
    # original. ZIP64 is handled by ZipFile; bound expansion without trusting it.
    if archive.stat().st_size > size + max(1024 * 1024, size // 100 + 65536):
        raise OriginalMismatchError("download archive exceeds single-item bound")
    with zipfile.ZipFile(archive) as bundle:
        items = bundle.infolist()
        if (
            len(items) != 1
            or items[0].filename != filename
            or items[0].is_dir()
            or items[0].file_size != size
            or (items[0].external_attr >> 16) & 0o170000 == 0o120000
        ):
            raise OriginalMismatchError("download archive does not contain exactly the expected item")
        with bundle.open(items[0]) as stream:
            copy_original(stream, destination, size, digest)


def probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(  # noqa: S603 - fixed executable and private canary path
        ["/usr/bin/ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        check=True,
        timeout=60,
    )
    raw = json.loads(result.stdout)
    video = next(item for item in raw["streams"] if item["codec_type"] == "video")
    return {
        "width": video["width"],
        "height": video["height"],
        "codec": video["codec_name"],
        "duration": float(raw["format"]["duration"]),
        "fps": video["r_frame_rate"],
        "audio_streams": sum(item["codec_type"] == "audio" for item in raw["streams"]),
    }


def main() -> int:
    from playwright.sync_api import sync_playwright

    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix="cloud-verify-", dir="/work"))
    try:
        expected = json.loads(os.environ["CANARY_EXPECTED"])
        archive = root / "download.zip"
        original = root / "cloud-original.mp4"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.goto(os.environ["PHOTOS_SHARE_URL"], wait_until="domcontentloaded", timeout=60000)
            page.get_by_role("button", name="More options", exact=True).click(timeout=30000)
            with page.expect_download(timeout=120000) as pending:
                page.get_by_role("menuitem", name="Download all", exact=True).click()
            download = pending.value
            download.save_as(archive)
            browser_version = browser.version
            browser.close()
        extract_original(archive, original, expected["filename"], expected["size"], expected["sha256"])
        media = probe(original)
        if any(media[key] != expected[key] for key in ("width", "height", "codec", "duration")):
            raise OriginalMismatchError("cloud media metadata mismatch")
        if media["width"] < 3840 or media["height"] < 2160:
            raise OriginalMismatchError("cloud video is below 4K")
        subprocess.run(  # noqa: S603 - fixed executable and private canary path
            ["/usr/bin/ffmpeg", "-v", "error", "-xerror", "-i", str(original), "-f", "null", "-"],
            capture_output=True,
            check=True,
            timeout=180,
        )
        report = {
            "artifact_directory": str(root),
            "original": "PASS",
            "method": "anonymous browser / sample-only album / Download all",
            "independent_of_guest_source": True,
            "archive_items": 1,
            "filename": expected["filename"],
            "size": original.stat().st_size,
            "sha256": expected["sha256"],
            "sha256_match": True,
            "media": media,
            "full_decode": "PASS",
            "browser_version": browser_version,
            "range_support": "NOT_ASSESSED",
            "used_m22": False,
        }
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "original": "FAIL" if isinstance(exc, (OriginalMismatchError, zipfile.BadZipFile)) else "OPEN",
                    "error_type": type(exc).__name__,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
