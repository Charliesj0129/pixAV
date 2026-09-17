#!/usr/bin/env python3
"""Archive list/zset DLQs and import only the latest payload per task.

The default is a read-only dry run. ``--apply`` renames legacy keys with a UTC
timestamp, applies a 30-day TTL, and populates the bounded deduplicated store.
No archived failure is replayed automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from typing import Any, cast

from pixav.config import get_settings
from pixav.shared.dead_letter import DeadLetterStore
from pixav.shared.redis_client import create_redis


def _decode_payload(raw: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("task_id") else None


async def _run(apply: bool) -> int:  # noqa: C901
    settings = get_settings()
    redis = await create_redis(settings)
    client = cast(Any, redis)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sources = {
        "download": [settings.queue_download_dlq],
        "upload": [settings.queue_upload_dlq, f"{settings.queue_upload}:dlq:replay"],
    }
    latest: dict[str, dict[str, dict[str, Any]]] = {"download": {}, "upload": {}}
    archived: list[tuple[str, str, int]] = []
    try:
        for stage, keys in sources.items():
            for key in keys:
                key_type = await client.type(key)
                if key_type == "none":
                    continue
                raw_items: list[str]
                if key_type == "list":
                    raw_items = list(await client.lrange(key, 0, -1))
                elif key_type == "zset":
                    raw_items = list(await client.zrange(key, 0, -1))
                else:
                    print(f"skip unsupported legacy key type {key_type}: {key}")
                    continue
                for raw in raw_items:
                    payload = _decode_payload(raw)
                    if payload is not None:
                        latest[stage][str(payload["task_id"])] = payload
                archived.append((key, f"{key}:legacy:{stamp}", len(raw_items)))

        print(
            json.dumps(
                {
                    "mode": "apply" if apply else "dry-run",
                    "legacy": [{"key": old, "archive": new, "items": count} for old, new, count in archived],
                    "unique_tasks": {stage: len(items) for stage, items in latest.items()},
                },
                indent=2,
                sort_keys=True,
            )
        )
        if not apply:
            return 0

        for old, new, _count in archived:
            await redis.rename(old, new)
            await redis.expire(new, settings.dlq_retention_days * 86400)
        store = DeadLetterStore(
            redis,
            retention_days=settings.dlq_retention_days,
            max_items_per_stage=settings.dlq_max_items_per_stage,
        )
        for stage, items in latest.items():
            for payload in items.values():
                await store.put(stage, payload)
        return 0
    finally:
        await redis.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.apply)))


if __name__ == "__main__":
    main()
