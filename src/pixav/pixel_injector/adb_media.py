"""Copy a verified Linux media inode through ADB while its child holds the lock.

This small tools helper has no account, Docker, UI or domain dependencies. The
caller owns the device fence and journals the intent before invoking it. A
failed or interrupted copy remains an unknown effect for reconciliation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import NoReturn

from pixav.shared.media_file import file_version, open_media_file

SourceVersion = tuple[int, int, int, int, int]


def push_locked(
    source: Path,
    destination: str,
    serial: str,
    *,
    digest: str,
    size: int,
    version: SourceVersion,
    timeout: int,
) -> None:
    """Verify both aliases, then pass the locked inode to the actual reader."""
    if (
        not source.is_absolute()
        or ".." in source.parts
        or not source.name.startswith("pixav-")
        or not source.name.endswith(".mp4")
        or len(source.name) > 255
        or destination != "/data/local/tmp/" + source.name
        or not re.fullmatch(r"(?:127\.0\.0\.1:5555|emulator-[0-9]{4,5})", serial)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
        or type(size) is not int
        or size <= 0
        or len(version) != 5
        or any(type(value) is not int or value < 0 for value in version)
        or type(timeout) is not int
        or not 1 <= timeout <= 21600
    ):
        raise ValueError("ADB_MEDIA_BINDING_INVALID")
    binary = shutil.which("adb")
    if binary is None:
        raise ValueError("ADB_MEDIA_EXECUTABLE_UNAVAILABLE")
    with open_media_file(source) as opened:
        if file_version(os.fstat(opened.descriptor)) != version or opened.facts() != (digest, size):
            raise ValueError("ADB_MEDIA_SOURCE_CHANGED")
        subprocess.run(  # noqa: S603 - resolved tools ADB, validated target/stage, inherited locked descriptor; no shell
            [binary, "-s", serial, "push", f"/proc/self/fd/{opened.descriptor}", destination],
            pass_fds=(opened.descriptor,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
        opened.assert_current()
        if file_version(os.fstat(opened.descriptor)) != version or opened.facts() != (digest, size):
            raise ValueError("ADB_MEDIA_SOURCE_CHANGED")


class _PrivateArguments(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's normal diagnostics can echo private filenames or argv.
        raise ValueError("ADB_MEDIA_ARGUMENTS_INVALID")


def main() -> int:
    parser = _PrivateArguments(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--source-version", nargs=5, type=int, required=True)
    parser.add_argument("--timeout", type=int, required=True)
    try:
        args = parser.parse_args()
        push_locked(
            args.source,
            args.destination,
            args.serial,
            digest=args.sha256,
            size=args.size,
            version=tuple(args.source_version),
            timeout=args.timeout,
        )
    except Exception:
        print(json.dumps({"status": "REVIEW_REQUIRED", "reason_code": "ADB_MEDIA_COPY_UNCONFIRMED"}))
        return 2
    print(json.dumps({"status": "COPIED"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
