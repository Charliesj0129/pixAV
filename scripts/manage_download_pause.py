#!/usr/bin/env python3
"""Inspect or safely resume the latched download disk-safety pause."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from pixav.config import get_settings
from pixav.shared.disk import build_download_space_guard
from pixav.shared.redis_client import create_redis


async def _run(command: str) -> int:
    settings = get_settings()
    redis = await create_redis(settings)
    try:
        guard = build_download_space_guard(settings, redis)
        if command == "resume":
            status = await guard.resume()
        else:
            status = await guard.status()
        print(json.dumps(asdict(status), indent=2, sort_keys=True))
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
