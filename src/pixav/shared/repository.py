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

from pixav.shared.enums import AccountStatus, TaskState, VideoStatus
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

    async def find_by_info_hash(self, info_hash: str) -> Video | None:
        """Return a video matching the given info_hash, or None."""
        row = await self._pool.fetchrow(
            "SELECT * FROM videos WHERE info_hash = $1",
            info_hash,
        )
        if row is None:
            return None
        return _video_from_row(row)

    async def insert(self, video: Video) -> Video:
        """Insert a new video row and return the persisted model."""
        row = await self._pool.fetchrow(
            """
            INSERT INTO videos (id, title, magnet_uri, local_path, share_url,
                                cdn_url, status, metadata_json, info_hash, quality_score, tags, embedding, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12, $13, $14)
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
            video.info_hash,
            video.quality_score,
            video.tags,
            video.embedding,
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

    async def update_download_result(
        self,
        video_id: uuid.UUID,
        *,
        local_path: str,
        metadata_json: str | None = None,
    ) -> None:
        """Persist local output path and optional metadata after download."""
        await self._pool.execute(
            """
            UPDATE videos
               SET local_path = $1,
                   metadata_json = COALESCE($2::jsonb, metadata_json),
                   status = $3,
                   updated_at = $4
             WHERE id = $5
            """,
            local_path,
            metadata_json,
            VideoStatus.DOWNLOADED.value,
            _utc_now(),
            video_id,
        )

    async def update_upload_result(
        self,
        video_id: uuid.UUID,
        *,
        share_url: str,
    ) -> None:
        """Persist share URL after a successful upload."""
        await self._pool.execute(
            """
            UPDATE videos
               SET share_url = $1,
                   status = $2,
                   updated_at = $3
             WHERE id = $4
            """,
            share_url,
            VideoStatus.AVAILABLE.value,
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

    async def update_embedding(self, video_id: uuid.UUID, embedding: list[float]) -> None:
        """Update the embedding vector for a video."""
        await self._pool.execute(
            "UPDATE videos SET embedding = $1 WHERE id = $2",
            embedding,
            video_id,
        )

    async def find_missing_embeddings(self, limit: int = 100) -> list[Video]:
        """Find videos that do not have an embedding yet."""
        rows = await self._pool.fetch(
            "SELECT * FROM videos WHERE embedding IS NULL ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return [_video_from_row(row) for row in rows]

    async def search(self, query: str, query_embedding: list[float], limit: int = 20) -> list[Video]:
        """Hybrid search using RRF (Reciprocal Rank Fusion) of Semantic + Keyword search.

        Args:
            query: The raw text query for keyword matching.
            query_embedding: The embedding vector of the query for semantic matching.
            limit: Max results to return.
        """
        rows = await self._pool.fetch(
            """
            WITH semantic AS (
                SELECT id, RANK() OVER (ORDER BY embedding <=> $2) as rank_vector
                FROM videos
                WHERE status = 'available'
                ORDER BY embedding <=> $2
                LIMIT 100
            ),
            keyword AS (
                SELECT id, RANK() OVER (ORDER BY ts_rank_cd(search_text, websearch_to_tsquery('simple', $1)) DESC) as rank_keyword
                FROM videos
                WHERE status = 'available'
                  AND search_text @@ websearch_to_tsquery('simple', $1)
                ORDER BY ts_rank_cd(search_text, websearch_to_tsquery('simple', $1)) DESC
                LIMIT 100
            )
            SELECT v.*,
                   COALESCE(1.0 / (60 + s.rank_vector), 0.0) +
                   COALESCE(1.0 / (60 + k.rank_keyword), 0.0) AS rrf_score
            FROM videos v
            LEFT JOIN semantic s ON v.id = s.id
            LEFT JOIN keyword k ON v.id = k.id
            WHERE s.id IS NOT NULL OR k.id IS NOT NULL
            ORDER BY rrf_score DESC
            LIMIT $3
            """,
            query,
            query_embedding,
            limit,
        )
        # rrf_score is ignored by model_validate (extra fields)
        return [_video_from_row(row) for row in rows]


class AccountRepository:
    """Write-side operations for account usage and cooldown controls."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def release_expired_cooldowns(self) -> int:
        """Reactivate cooldown accounts that are ready to be reused."""
        tag = await self._pool.execute(
            """
            UPDATE accounts
               SET status = $1,
                   cooldown_until = NULL,
                   lease_expires_at = NULL,
                   daily_uploaded_bytes = 0,
                   quota_reset_at = date_trunc('day', now()) + interval '1 day'
             WHERE status = $2
               AND cooldown_until IS NOT NULL
               AND cooldown_until <= now()
            """,
            AccountStatus.ACTIVE.value,
            AccountStatus.COOLDOWN.value,
        )
        return _rows_from_tag(tag)

    async def apply_upload_usage(self, account_id: uuid.UUID, uploaded_bytes: int) -> None:
        """Add uploaded bytes for an account and enter cooldown on quota exhaustion."""
        safe_bytes = max(uploaded_bytes, 0)
        await self._pool.execute(
            """
            UPDATE accounts
               SET daily_uploaded_bytes = CASE
                       WHEN quota_reset_at <= now() THEN $2
                       ELSE daily_uploaded_bytes + $2
                   END,
                   quota_reset_at = CASE
                       WHEN quota_reset_at <= now() THEN date_trunc('day', now()) + interval '1 day'
                       ELSE quota_reset_at
                   END,
                   last_used_at = now(),
                   status = CASE
                       WHEN (
                            CASE
                                WHEN quota_reset_at <= now() THEN $2
                                ELSE daily_uploaded_bytes + $2
                            END
                       ) >= daily_quota_bytes THEN $3
                       ELSE status
                   END,
                   cooldown_until = CASE
                       WHEN (
                            CASE
                                WHEN quota_reset_at <= now() THEN $2
                                ELSE daily_uploaded_bytes + $2
                            END
                       ) >= daily_quota_bytes THEN quota_reset_at
                       ELSE NULL
                   END,
                   lease_expires_at = NULL
             WHERE id = $1
            """,
            account_id,
            safe_bytes,
            AccountStatus.COOLDOWN.value,
        )


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

    async def set_retry(
        self,
        task_id: uuid.UUID,
        retries: int,
        *,
        state: TaskState = TaskState.PENDING,
        error_message: str | None = None,
    ) -> None:
        """Persist retry count with state/error updates.

        Used by workers that requeue tasks after transient failures.
        """
        await self._pool.execute(
            """
            UPDATE tasks
               SET retries = $1,
                   state = $2,
                   error_message = $3,
                   updated_at = $4
             WHERE id = $5
            """,
            retries,
            state.value,
            error_message,
            _utc_now(),
            task_id,
        )

    async def route_to_queue(
        self,
        task_id: uuid.UUID,
        *,
        queue_name: str,
        state: TaskState = TaskState.PENDING,
    ) -> None:
        """Route task to a queue while forcing a target state.

        Used when a worker finishes one stage and hands over to the next stage.
        """
        await self._pool.execute(
            """
            UPDATE tasks
               SET queue_name = $1,
                   state = $2,
                   error_message = NULL,
                   updated_at = $3
             WHERE id = $4
            """,
            queue_name,
            state.value,
            _utc_now(),
            task_id,
        )

    async def assign_account(
        self,
        task_id: uuid.UUID,
        account_id: str,
    ) -> None:
        """Bind a Google account to a task for upload scheduling."""
        await self._pool.execute(
            """
            UPDATE tasks
               SET account_id = $1,
                   updated_at = $2
             WHERE id = $3
            """,
            uuid.UUID(account_id),
            _utc_now(),
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

    async def has_open_task(self, video_id: uuid.UUID) -> bool:
        """Return True when a video already has an in-flight task.

        Open states are transient/non-terminal states that indicate the
        pipeline is already processing this video.
        """
        open_states = [
            TaskState.PENDING.value,
            TaskState.DOWNLOADING.value,
            TaskState.REMUXING.value,
            TaskState.UPLOADING.value,
            TaskState.VERIFYING.value,
        ]
        exists = await self._pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM tasks
                 WHERE video_id = $1
                   AND state = ANY($2::text[])
            )
            """,
            video_id,
            open_states,
        )
        return bool(exists)


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
    # data.pop("embedding", None) # Do not pop embedding, we want it in the model now
    return Video.model_validate(data)


def _task_from_row(row: asyncpg.Record) -> Task:
    """Convert an asyncpg Record to a Task model."""
    data: dict[str, Any] = dict(row)
    data["state"] = TaskState(data["state"])
    return Task.model_validate(data)


def _rows_from_tag(tag: str) -> int:
    parts = tag.split()
    if len(parts) < 2:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0
