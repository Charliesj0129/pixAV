#!/usr/bin/env python3
"""Inspect or safely resume the latched download disk-safety pause."""

from __future__ import annotations

import argparse
import asyncio
import json

from pixav.config import get_settings
from pixav.shared.disk import DownloadSpaceGuard
from pixav.shared.redis_client import create_redis


async def _run(command: str) -> int:
    settings = get_settings()
    redis = await create_redis(settings)
    guard = DownloadSpaceGuard(
        redis,
        path=settings.download_dir,
        pause_key=settings.download_pause_key,
        min_free_bytes=settings.download_min_free_bytes,
        min_free_percent=settings.download_min_free_percent,
    )
    try:
        if command == "resume":
            status = await guard.resume()
        else:
            status = await guard.check_and_latch()
        print(json.dumps(status.__dict__, indent=2, sort_keys=True))
        return 0
    except RuntimeError as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, indent=2))
        return 2
    finally:
        await redis.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("status", "resume"))
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.command)))


if __name__ == "__main__":
    main()
