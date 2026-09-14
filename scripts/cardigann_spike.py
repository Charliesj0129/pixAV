#!/usr/bin/env python3
"""Command line over :mod:`pixav.first_4k.discovery`.

The collection logic moved into the package because the single-film run depends
on it; this file keeps the standalone entry point and the import path that
``cardigann_parity`` and the fixture tests already use.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from pixav.first_4k.discovery import (
    InputCrawler,
    attachment_reference,
    board_index_path,
    capture_torrents,
    collect,
    collect_boards,
    fetch_attachment,
    looks_like_torrent,
    public_thread_path,
    sanitized_age_gate,
    sanitized_fixture,
)
from pixav.shared.backup_files import create_backup_file

__all__ = [
    "InputCrawler",
    "attachment_reference",
    "board_index_path",
    "capture_torrents",
    "collect",
    "collect_boards",
    "fetch_attachment",
    "looks_like_torrent",
    "main",
    "public_thread_path",
    "sanitized_age_gate",
    "sanitized_fixture",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", default="https://www.sehuatang.org/forum-103-1.html")
    parser.add_argument("--cookie-file", type=Path, default=Path("secrets/sehuatang-cookies.txt"))
    parser.add_argument("--flaresolverr", default="http://127.0.0.1:18191")
    parser.add_argument("--max-threads", type=int, choices=range(1, 11), default=5)
    parser.add_argument("--fetch-attachments", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)  # Upstream HTTP errors can contain query/cookie material.
    try:
        result = asyncio.run(collect(args))
    except Exception as exc:
        result = {
            "status": "BLOCKED",
            "reason": "input_fetch_failed",
            "error_type": type(exc).__name__,
            "parity": "NOT_RUN",
        }
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with create_backup_file(args.output / "evidence.json") as handle:
        json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
