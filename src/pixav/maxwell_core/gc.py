"""Orphan task cleanup and garbage collection."""

from __future__ import annotations

import logging
from datetime import timedelta

import asyncpg

from pixav.shared.enums import TaskState

logger = logging.getLogger(__name__)

# Tasks stuck in transient states for longer than this are considered orphans
_DEFAULT_ORPHAN_AGE = timedelta(hours=2)

# Transient states that should not persist indefinitely
_TRANSIENT_STATES = (
    TaskState.DOWNLOADING,
    TaskState.REMUXING,
    TaskState.UPLOADING,
    TaskState.VERIFYING,
)


class OrphanTaskCleaner:
    """Detect and clean up orphaned tasks.

    A task is considered orphaned if it has been in a transient state
    (downloading, remuxing, uploading, verifying) for longer than
    ``max_age`` without progressing.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        max_age: timedelta = _DEFAULT_ORPHAN_AGE,
    ) -> None:
        self._pool = pool
        self._max_age = max_age

    async def cleanup(self) -> int:
        """Find orphaned tasks and mark them as FAILED.

        Returns:
            Number of orphaned tasks that were cleaned up.
        """
        transient_values = [s.value for s in _TRANSIENT_STATES]

        result = await self._pool.execute(
            """
            UPDATE tasks
               SET state = $1,
                   error_message = 'orphan cleanup: stuck in transient state',
                   updated_at = now()
             WHERE state = ANY($2::text[])
               AND updated_at < now() - $3::interval
            """,
            TaskState.FAILED.value,
            transient_values,
            self._max_age,
        )

        # result is like "UPDATE N"
        count = _parse_update_count(result)
        if count > 0:
            logger.warning("cleaned up %d orphaned tasks", count)
        else:
            logger.debug("no orphaned tasks found")
        return count

    async def cleanup_expired_videos(self) -> int:
        """Mark videos with expired share URLs as ``expired``.

        Returns:
            Number of videos marked as expired.
        """
        result = await self._pool.execute("""
            UPDATE videos
               SET status = 'expired', updated_at = now()
             WHERE status = 'available'
               AND share_url IS NOT NULL
               AND updated_at < now() - interval '30 days'
            """)
        count = _parse_update_count(result)
        if count > 0:
            logger.info("marked %d videos as expired", count)
        return count


def _parse_update_count(result: str) -> int:
    """Parse PostgreSQL UPDATE command result to extract row count."""
    # asyncpg returns strings like "UPDATE 5"
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0
