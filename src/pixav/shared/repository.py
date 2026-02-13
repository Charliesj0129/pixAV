"""Async repository layer for PostgreSQL CRUD operations.

Provides VideoRepository and TaskRepository with basic operations
used by all pipeline modules.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg

from pixav.shared.enums import TaskState, VideoStatus
from pixav.shared.models import Task, Video

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class VideoRepository:
    """CRUD operations for the ``videos`` table."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def find_by_id(self, video_id: uuid.UUID) -> Video | None:
        """Fetch a single video by primary key."""
        row = await self._pool.fetchrow(
            "SELECT * FROM videos WHERE id = $1",
            video_id,
        )
        if row is None:
            return None
        return _video_from_row(row)

    async def find_by_magnet(self, magnet_uri: str) -> Video | None:
        """Return a video matching the given magnet URI, or None."""
        row = await self._pool.fetchrow(
            "SELECT * FROM videos WHERE magnet_uri = $1",
            magnet_uri,
        )
        if row is None:
            return None
        return _video_from_row(row)

    async def insert(self, video: Video) -> Video:
        """Insert a new video row and return the persisted model."""
        row = await self._pool.fetchrow(
            """
            INSERT INTO videos (id, title, magnet_uri, local_path, share_url,
                                cdn_url, status, metadata_json, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
            RETURNING *
            """,
            video.id,
            video.title,
            video.magnet_uri,
            video.local_path,
            video.share_url,
            video.cdn_url,
            video.status.value,
            video.metadata_json,
            video.created_at,
            video.updated_at,
        )
        logger.info("inserted video %s (%s)", video.id, video.title)
        return _video_from_row(row)

    async def update_status(
        self,
        video_id: uuid.UUID,
        new_status: VideoStatus,
    ) -> None:
        """Set the status and bump updated_at for a video."""
        await self._pool.execute(
            "UPDATE videos SET status = $1, updated_at = $2 WHERE id = $3",
            new_status.value,
            _utc_now(),
            video_id,
        )

    async def count_by_status(self, status: VideoStatus) -> int:
        """Return the number of videos with the given status."""
        val = await self._pool.fetchval(
            "SELECT count(*) FROM videos WHERE status = $1",
            status.value,
        )
        return int(val)


class TaskRepository:
    """CRUD operations for the ``tasks`` table."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def find_by_id(self, task_id: uuid.UUID) -> Task | None:
        """Fetch a single task by primary key."""
        row = await self._pool.fetchrow(
            "SELECT * FROM tasks WHERE id = $1",
            task_id,
        )
        if row is None:
            return None
        return _task_from_row(row)

    async def insert(self, task: Task) -> Task:
        """Insert a new task row and return the persisted model."""
        row = await self._pool.fetchrow(
            """
            INSERT INTO tasks (id, video_id, account_id, state, queue_name,
                               retries, max_retries, error_message, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING *
            """,
            task.id,
            task.video_id,
            task.account_id,
            task.state.value,
            task.queue_name,
            task.retries,
            task.max_retries,
            task.error_message,
            task.created_at,
            task.updated_at,
        )
        logger.info("inserted task %s for video %s", task.id, task.video_id)
        return _task_from_row(row)

    async def update_state(
        self,
        task_id: uuid.UUID,
        new_state: TaskState,
        *,
        error_message: str | None = None,
    ) -> None:
        """Update task state, bump updated_at, optionally set error_message."""
        await self._pool.execute(
            """
            UPDATE tasks
               SET state = $1, updated_at = $2, error_message = $3
             WHERE id = $4
            """,
            new_state.value,
            _utc_now(),
            error_message,
            task_id,
        )

    async def count_by_state(self, state: TaskState) -> int:
        """Return the number of tasks with the given state."""
        val = await self._pool.fetchval(
            "SELECT count(*) FROM tasks WHERE state = $1",
            state.value,
        )
        return int(val)

    async def list_pending(self, limit: int = 100) -> list[Task]:
        """Return pending tasks ordered by oldest first."""
        rows = await self._pool.fetch(
            """
            SELECT * FROM tasks
             WHERE state = $1
             ORDER BY created_at ASC
             LIMIT $2
            """,
            TaskState.PENDING.value,
            limit,
        )
        return [_task_from_row(row) for row in rows]


# ── Row → Model helpers ────────────────────────────────────────


def _video_from_row(row: asyncpg.Record) -> Video:
    """Convert an asyncpg Record to a Video model."""
    data: dict[str, Any] = dict(row)
    # status comes back as text; coerce to enum
    data["status"] = VideoStatus(data["status"])
    # metadata_json may be stored as dict by asyncpg's jsonb decoder
    if isinstance(data.get("metadata_json"), dict):
        data["metadata_json"] = json.dumps(data["metadata_json"])
    # drop columns that don't map to the model (e.g. embedding)
    data.pop("embedding", None)
    return Video.model_validate(data)


def _task_from_row(row: asyncpg.Record) -> Task:
    """Convert an asyncpg Record to a Task model."""
    data: dict[str, Any] = dict(row)
    data["state"] = TaskState(data["state"])
    return Task.model_validate(data)
