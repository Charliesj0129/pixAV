#!/usr/bin/env python3
"""Copy a private cookie export into the dedicated spike secret volume via stdin."""

from __future__ import annotations

import argparse
import stat
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookie-file", type=Path, default=Path("secrets/sehuatang-cookies.txt"))
    args = parser.parse_args()
    source = args.cookie_file
    if source.is_symlink() or not source.is_file() or stat.S_IMODE(source.stat().st_mode) != 0o600:
        parser.error("cookie export must be a regular non-symlink 0600 file")
    # Fixed project volume, no production mount. Cookie bytes never enter argv or logs.
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607 -- fixed Docker CLI
            "docker",
            "run",
            "--rm",
            "-i",
            "--mount",
            "type=volume,source=pixav-cardigann_cardigann_cookie,target=/private",
            "redis:7-alpine@sha256:8b81dd37ff027bec4e516d41acfbe9fe2460070dc6d4a4570a2ac5b9d59df065",
            "sh",
            "-c",
            "umask 077; cat > /private/sehuatang_cookie && chown 1000:1000 /private/sehuatang_cookie",
        ],
        input=source.read_bytes(),
        capture_output=True,
        check=False,
    )
    if result.returncode:
        parser.exit(2, "cookie provisioning failed (details withheld)\n")
    print("Isolated cookie volume provisioned; consumer mount is read-only.")


if __name__ == "__main__":
    main()
