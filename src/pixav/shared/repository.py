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
from pixav.shared.models import Account, SourceCandidate, Task, Video

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
                                status, metadata_json, info_hash, quality_score, tags, embedding,
                                created_at, updated_at, local_cleanup_after)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12, $13, $14)
            RETURNING *
            """,
            video.id,
            video.title,
            video.magnet_uri,
            video.local_path,
            video.share_url,
            video.status.value,
            video.metadata_json,
            video.info_hash,
            video.quality_score,
            video.tags,
            video.embedding,
            video.created_at,
            video.updated_at,
            video.local_cleanup_after,
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
        title: str | None = None,
        quality_score: int | None = None,
    ) -> None:
        """Persist local output path and optional metadata after download."""
        await self._pool.execute(
            """
            UPDATE videos
               SET local_path = $1,
                   metadata_json = COALESCE(metadata_json, '{}'::jsonb) || COALESCE($2::jsonb, '{}'::jsonb),
                   title = COALESCE(NULLIF(btrim($3), ''), title),
                   quality_score = COALESCE($4, quality_score),
                   status = $5,
                   updated_at = $6
             WHERE id = $7
            """,
            local_path,
            metadata_json,
            title,
            quality_score,
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
                   local_cleanup_after = CASE
                       WHEN $1 LIKE 'pixav-local://%' THEN NULL
                       ELSE $3::timestamptz + interval '24 hours'
                   END,
                   updated_at = $3
             WHERE id = $4
            """,
            share_url,
            VideoStatus.AVAILABLE.value,
            _utc_now(),
            video_id,
        )

    async def schedule_terminal_cleanup(self, video_id: uuid.UUID, *, retention_days: int = 7) -> None:
        """Retain failed local files for diagnostics, then make them janitor-eligible."""
        await self._pool.execute(
            """
            UPDATE videos
               SET local_cleanup_after = CASE
                       WHEN local_path IS NULL OR share_url LIKE 'pixav-local://%' THEN NULL
                       ELSE now() + ($1 * interval '1 day')
                   END,
                   updated_at = now()
             WHERE id = $2
            """,
            max(1, retention_days),
            video_id,
        )

    async def update_source(self, video_id: uuid.UUID, *, magnet_uri: str, info_hash: str | None) -> None:
        """Repoint a video at a different source candidate."""
        await self._pool.execute(
            """
            UPDATE videos
               SET magnet_uri = $2,
                   info_hash = $3,
                   updated_at = now()
             WHERE id = $1
            """,
            video_id,
            magnet_uri,
            info_hash,
        )
        logger.info("video %s repointed to source %s", video_id, magnet_uri[:60])

    async def clear_local_path(self, video_id: uuid.UUID) -> None:
        await self._pool.execute(
            """
            UPDATE videos
               SET local_path = NULL,
                   local_cleanup_after = NULL,
                   updated_at = now()
             WHERE id = $1
            """,
            video_id,
        )

    async def update_metadata_section(self, video_id: uuid.UUID, section: str, value: dict[str, Any]) -> None:
        """Merge one provenance section without replacing sibling sections."""
        await self._pool.execute(
            """
            UPDATE videos
               SET metadata_json = jsonb_set(
                       COALESCE(metadata_json, '{}'::jsonb),
                       ARRAY[$1]::text[],
                       $2::jsonb,
                       true
                   ),
                   updated_at = now()
             WHERE id = $3
            """,
            section,
            json.dumps(value),
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


class SourceCandidateRepository:
    """CRUD operations for the ``source_candidates`` table.

    Cooldown, not deletion: a swarm that is dead today may be alive next week,
    so an exhausted candidate is parked with an ``unavailable_until`` rather
    than removed. This mirrors the account cooldown in AccountRepository.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def register(
        self,
        video_id: uuid.UUID,
        *,
        magnet_uri: str,
        info_hash: str | None = None,
        origin: str = "sehuatang",
        quality_score: int = 0,
    ) -> None:
        """Record a source for a video, ignoring one already known."""
        await self._pool.execute(
            """
            INSERT INTO source_candidates (video_id, magnet_uri, info_hash, origin, quality_score)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (video_id, magnet_uri) DO NOTHING
            """,
            video_id,
            magnet_uri,
            info_hash,
            origin,
            quality_score,
        )

    async def mark_unavailable(
        self,
        video_id: uuid.UUID,
        magnet_uri: str,
        *,
        reason: str,
        cooldown_hours: int = 6,
    ) -> None:
        """Cool a candidate down after its swarm failed to deliver."""
        await self._pool.execute(
            """
            UPDATE source_candidates
               SET state = 'unavailable',
                   unavailable_until = now() + make_interval(hours => $3),
                   attempts = attempts + 1,
                   last_error = $4,
                   updated_at = now()
             WHERE video_id = $1
               AND magnet_uri = $2
            """,
            video_id,
            magnet_uri,
            cooldown_hours,
            reason[:500],
        )
        logger.warning("source candidate cooled down for %sh: video=%s", cooldown_hours, video_id)

    async def mark_succeeded(self, video_id: uuid.UUID, magnet_uri: str) -> None:
        """Record the candidate that actually delivered the media."""
        await self._pool.execute(
            """
            UPDATE source_candidates
               SET state = 'succeeded',
                   unavailable_until = NULL,
                   updated_at = now()
             WHERE video_id = $1
               AND magnet_uri = $2
            """,
            video_id,
            magnet_uri,
        )

    async def next_candidate(self, video_id: uuid.UUID, *, exclude_magnet: str | None = None) -> SourceCandidate | None:
        """Return the best source still worth trying, or None when exhausted."""
        row = await self._pool.fetchrow(
            """
            SELECT * FROM source_candidates
             WHERE video_id = $1
               AND (state IN ('pending', 'succeeded') OR
                    (state = 'unavailable' AND unavailable_until <= now()))
               AND ($2::text IS NULL OR magnet_uri <> $2)
             ORDER BY quality_score DESC, lower(origin), info_hash, magnet_uri
             LIMIT 1
            """,
            video_id,
            exclude_magnet,
        )
        if row is None:
            return None
        return SourceCandidate.model_validate(dict(row))

    async def release_expired_cooldowns(self) -> int:
        """Return cooled-down candidates to the pool once their window closes."""
        tag = await self._pool.execute("""
            UPDATE source_candidates
               SET state = 'pending',
                   unavailable_until = NULL,
                   updated_at = now()
             WHERE state = 'unavailable'
               AND unavailable_until IS NOT NULL
               AND unavailable_until <= now()
            """)
        released = _rows_from_tag(str(tag))
        if released:
            logger.info("released %d source candidate cooldown(s)", released)
        return released


class AccountRepository:
    """Write-side operations for account usage and cooldown controls."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def find_by_id(self, account_id: uuid.UUID) -> Account | None:
        """Fetch a single account by primary key."""
        row = await self._pool.fetchrow(
            "SELECT * FROM accounts WHERE id = $1",
            account_id,
        )
        if row is None:
            return None
        data = dict(row)
        data["status"] = AccountStatus(data["status"])
        return Account.model_validate(data)

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
                               local_path, share_url, retries, max_retries, error_message,
                               trace_id, created_at, updated_at, retry_not_before)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            RETURNING *
            """,
            task.id,
            task.video_id,
            task.account_id,
            task.state.value,
            task.queue_name,
            task.local_path,
            task.share_url,
            task.retries,
            task.max_retries,
            task.error_message,
            task.trace_id,
            task.created_at,
            task.updated_at,
            task.retry_not_before,
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
        retry_not_before: datetime | None = None,
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
                   retry_not_before = $4,
                   updated_at = $5
             WHERE id = $6
            """,
            retries,
            state.value,
            error_message,
            retry_not_before,
            _utc_now(),
            task_id,
        )

    async def claim_for_dispatch(
        self,
        task_id: uuid.UUID,
        *,
        next_state: TaskState,
        account_id: uuid.UUID | None = None,
    ) -> bool:
        """Atomically claim a pending task for dispatch.

        Returns:
            True when claim succeeds (task was pending), False otherwise.
        """
        tag = await self._pool.execute(
            """
            UPDATE tasks
               SET state = $1,
                   account_id = COALESCE($2::uuid, account_id),
                   error_message = NULL,
                   retry_not_before = NULL,
                   updated_at = $3
             WHERE id = $4
               AND state = $5
               AND (retry_not_before IS NULL OR retry_not_before <= now())
            """,
            next_state.value,
            account_id,
            _utc_now(),
            task_id,
            TaskState.PENDING.value,
        )
        return _rows_from_tag(tag) == 1

    async def release_dispatch_claim(
        self,
        task_id: uuid.UUID,
        *,
        error_message: str | None = None,
        clear_account: bool = False,
    ) -> None:
        """Return a claimed task back to pending after dispatch failure."""
        await self._pool.execute(
            """
            UPDATE tasks
               SET state = $1,
                   account_id = CASE WHEN $2 THEN NULL ELSE account_id END,
                   error_message = $3,
                   retry_not_before = NULL,
                   updated_at = $4
             WHERE id = $5
            """,
            TaskState.PENDING.value,
            clear_account,
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
                   retry_not_before = NULL,
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
               AND queue_name <> 'pixav:media-managed'
               AND (retry_not_before IS NULL OR retry_not_before <= now())
             ORDER BY created_at ASC
             LIMIT $2
            """,
            TaskState.PENDING.value,
            limit,
        )
        return [_task_from_row(row) for row in rows]

    async def replay(self, task_id: uuid.UUID, *, requested_by: str = "operator", reason: str | None = None) -> bool:
        """Start a new retry cycle and persist an immutable operator audit row."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT state, retries FROM tasks WHERE id = $1 FOR UPDATE", task_id)
                if row is None:
                    return False
                await conn.execute(
                    """
                    INSERT INTO task_replay_audit (task_id, previous_state, previous_retries, requested_by, reason)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    task_id,
                    row["state"],
                    row["retries"],
                    requested_by,
                    reason,
                )
                await conn.execute(
                    """
                    UPDATE tasks
                       SET state = 'pending', retries = 0, retry_not_before = now(),
                           error_message = NULL, updated_at = now()
                     WHERE id = $1
                    """,
                    task_id,
                )
        return True

    async def is_managed_video(self, video_id: uuid.UUID) -> bool:
        """Whether the managed execution authority has admitted this video."""
        if await self._pool.fetchval("SELECT to_regclass('public.workflow_tasks')") is None:
            return False
        return bool(await self._pool.fetchval("SELECT EXISTS(SELECT FROM workflow_tasks WHERE video_id=$1)", video_id))

    async def has_open_task(self, video_id: uuid.UUID) -> bool:
        """Return True when a video already has an in-flight task.

        Open states are transient/non-terminal states that indicate the
        pipeline is already processing this video.
        """
        open_states = [
            TaskState.PENDING.value,
            TaskState.DISPATCHED.value,
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
                   AND (state = ANY($2::text[]) OR queue_name = 'pixav:media-managed')
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
