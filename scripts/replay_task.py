#!/usr/bin/env python3
"""Manually start a fresh durable retry cycle for one terminal task."""

from __future__ import annotations

import argparse
import asyncio
import uuid

from pixav.config import get_settings
from pixav.shared.db import create_pool
from pixav.shared.dead_letter import DeadLetterStore
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import TaskRepository


async def _run(task_id: uuid.UUID, requested_by: str, reason: str | None) -> int:
    settings = get_settings()
    pool = await create_pool(settings)
    redis = await create_redis(settings)
    try:
        repo = TaskRepository(pool)
        task = await repo.find_by_id(task_id)
        if task is None:
            print(f"task not found: {task_id}")
            return 2
        if not await repo.replay(task_id, requested_by=requested_by, reason=reason):
            return 2
        stage = "upload" if task.queue_name == settings.queue_upload else "download"
        store = DeadLetterStore(
            redis,
            retention_days=settings.dlq_retention_days,
            max_items_per_stage=settings.dlq_max_items_per_stage,
        )
        await store.remove(stage, str(task_id))
        print(f"task {task_id} queued for a fresh six-retry cycle ({stage})")
        return 0
    finally:
        await redis.aclose()
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id", type=uuid.UUID)
    parser.add_argument("--requested-by", default="operator")
    parser.add_argument("--reason")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.task_id, args.requested_by, args.reason)))


if __name__ == "__main__":
    main()
