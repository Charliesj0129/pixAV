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
from contextlib import ExitStack
from pathlib import Path
from typing import IO, Any

from pixav.shared.media_file import (
    MediaFile,
    MediaFileChangedError,
    MediaFileUnsafeError,
    file_version,
    open_media_directory,
    open_media_file,
    open_media_output,
)


class OriginalMismatchError(ValueError):
    """Downloaded bytes or media cannot be the requested original."""


class OriginalWorkConflictError(ValueError):
    """Preserve changed work; this is not a provider integrity verdict."""


def copy_original(stream: IO[bytes], destination: Path, size: int, digest: str) -> None:
    """Accept exactly the expected original bytes; never publish a partial result."""
    destination = Path(os.path.abspath(destination))
    partial = destination.with_suffix(destination.suffix + ".part")
    if destination.exists() or destination.is_symlink():
        raise OriginalWorkConflictError("original destination already exists")
    try:
        _copy_original(stream, destination, partial, size, digest)
    except (OSError, MediaFileChangedError, MediaFileUnsafeError):
        raise OriginalWorkConflictError("COLD_ORIGINAL_WORK_REQUIRES_RECONCILIATION") from None


def _copy_original(stream: IO[bytes], destination: Path, partial: Path, size: int, digest: str) -> None:
    with open_media_output(partial, must_create=True) as output:
        try:
            measured = hashlib.sha256()
            count = 0
            with os.fdopen(os.dup(output.descriptor), "wb") as target:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    count += len(chunk)
                    if count > size:
                        raise OriginalMismatchError("cloud file exceeds source size")
                    target.write(chunk)
                    measured.update(chunk)
                target.flush()
                os.fsync(target.fileno())
            if count != size or measured.hexdigest() != digest or output.facts() != (digest, size):
                raise OriginalMismatchError("cloud original size or SHA-256 mismatch")
        except Exception:
            try:
                output.unlink()
            except (OSError, ValueError):
                raise OriginalWorkConflictError("COLD_ORIGINAL_WORK_REQUIRES_RECONCILIATION") from None
            raise
        # Interruptions retain owned partial work. Publication conflicts also
        # retain it; never replace a destination or remove a changed name.
        try:
            with open_media_directory(destination, exclusive=True) as directory:
                output.assert_current()
                os.link(
                    partial.name,
                    destination.name,
                    src_dir_fd=directory.descriptor,
                    dst_dir_fd=directory.descriptor,
                    follow_symlinks=False,
                )
                MediaFile(destination, output.descriptor, directory.descriptor, True).assert_current()
                output.assert_current()
                os.unlink(partial.name, dir_fd=directory.descriptor)
                os.fsync(directory.descriptor)
        except (OSError, ValueError):
            raise OriginalWorkConflictError("COLD_ORIGINAL_WORK_REQUIRES_RECONCILIATION") from None


def extract_original(archive: Path, destination: Path, filename: str, size: int, digest: str) -> None:
    """Download-all must contain exactly one regular item with the expected name."""
    # DEFLATE can expand incompressible data by more than 1 MiB for a 9.5 GB
    # original. ZIP64 is handled by ZipFile; bound expansion without trusting it.
    try:
        with open_media_file(archive) as source, source.reader() as reader:
            if os.fstat(source.descriptor).st_size > size + max(1024 * 1024, size // 100 + 65536):
                raise OriginalMismatchError("download archive exceeds single-item bound")
            with zipfile.ZipFile(reader) as bundle:
                _extract_item(bundle, destination, filename, size, digest)
            source.assert_current()
    except (OSError, MediaFileChangedError, MediaFileUnsafeError):
        raise OriginalWorkConflictError("COLD_ORIGINAL_WORK_REQUIRES_RECONCILIATION") from None


def _extract_item(bundle: zipfile.ZipFile, destination: Path, filename: str, size: int, digest: str) -> None:
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


def probe(path: Path | MediaFile) -> dict[str, Any]:
    with ExitStack() as files:
        source = path if isinstance(path, MediaFile) else files.enter_context(open_media_file(path))
        source.assert_current()
        before = file_version(os.fstat(source.descriptor))
        result = _probe(source)
        source.assert_current()
        if file_version(os.fstat(source.descriptor)) != before:
            raise OriginalWorkConflictError("COLD_ORIGINAL_CHANGED_DURING_PROBE")
        return result


def _probe(source: MediaFile) -> dict[str, Any]:
    result = subprocess.run(  # noqa: S603 - fixed executable and private canary path
        [
            "/usr/bin/ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            f"/proc/self/fd/{source.descriptor}",
        ],
        capture_output=True,
        check=True,
        timeout=60,
        pass_fds=(source.descriptor,),
    )
    if result.stderr:
        raise OriginalMismatchError("canary probe reported media errors")
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


def decode_original(path: Path | MediaFile, *, timeout: int = 21600) -> None:
    """Decode the complete provider original; diagnostics stay out of receipts."""
    with ExitStack() as files, tempfile.TemporaryFile() as errors:
        source = path if isinstance(path, MediaFile) else files.enter_context(open_media_file(path))
        source.assert_current()
        before = file_version(os.fstat(source.descriptor))
        result = subprocess.run(  # noqa: S603 - fixed image tool, argv only, verified local provider output
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-threads",
                "2",
                "-i",
                f"/proc/self/fd/{source.descriptor}",
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                # Preserve native frame timestamps, including VFR frames that
                # would otherwise collide on the nominal frame-rate grid.
                "-fps_mode:v",
                "passthrough",
                "-enc_time_base:v",
                "-1",
                # The discarded null sink still requires increasing DTS, even
                # when every source frame decoded successfully. Adjust only
                # its packet clock; keep every frame and all decoder errors.
                "-bsf:v",
                r"setts=ts=max(TS\,PREV_OUTDTS+1)",
                "-f",
                "null",
                "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=errors,
            timeout=timeout,
            check=False,
            pass_fds=(source.descriptor,),
        )
        source.assert_current()
        if file_version(os.fstat(source.descriptor)) != before:
            raise OriginalWorkConflictError("Photos original changed during complete decoding")
        if result.returncode or errors.tell():
            raise OriginalMismatchError("Photos original failed complete decoding")


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
        with open_media_file(original) as source:
            facts = source.facts()
            if facts != (expected["sha256"], expected["size"]):
                raise OriginalMismatchError("cloud original changed before verification")
            media = probe(source)
            if any(media[key] != expected[key] for key in ("width", "height", "codec", "duration")):
                raise OriginalMismatchError("cloud media metadata mismatch")
            if media["width"] < 3840 or media["height"] < 2160:
                raise OriginalMismatchError("cloud video is below 4K")
            decode_original(source, timeout=180)
            if source.facts() != facts:
                raise OriginalWorkConflictError("COLD_ORIGINAL_CHANGED_DURING_VERIFICATION")
            report = {
                "artifact_directory": str(root),
                "original": "PASS",
                "method": "anonymous browser / sample-only album / Download all",
                "independent_of_guest_source": True,
                "archive_items": 1,
                "filename": expected["filename"],
                "size": facts[1],
                "sha256": facts[0],
                "sha256_match": True,
                "media": media,
                "full_decode": "PASS",
                "browser_version": browser_version,
                "range_support": "NOT_ASSESSED",
                "used_m22": False,
            }
            source.assert_current()
            with (root / "report.json").open("x") as receipt:
                json.dump(report, receipt, indent=2)
                receipt.write("\n")
                receipt.flush()
                os.fsync(receipt.fileno())
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
    from pixav.pixel_injector.canary_download import main as entrypoint

    raise SystemExit(entrypoint())
