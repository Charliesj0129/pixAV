"""Task scheduling with LRU account selection."""

from __future__ import annotations

import logging
import uuid

import asyncpg

from pixav.shared.enums import AccountStatus

logger = logging.getLogger(__name__)


class LruAccountScheduler:
    """Select the next Google account using least-recently-used policy.

    Implements the ``TaskScheduler`` protocol.

    Queries the ``accounts`` table for active accounts ordered by
    ``last_used_at`` ascending (oldest first = least recently used).
    """

    def __init__(self, pool: asyncpg.Pool, *, lease_seconds: int = 600) -> None:
        self._pool = pool
        self._lease_seconds = lease_seconds

    async def next_account(self) -> str:
        """Return the account ID least recently used for uploading.

        Returns:
            Account UUID string.

        Raises:
            RuntimeError: If no active accounts are available.
        """
        await self._pool.execute(
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

        row = await self._pool.fetchrow(
            """
            WITH candidate AS (
                SELECT id
                  FROM accounts
                 WHERE status = $1
                   AND (cooldown_until IS NULL OR cooldown_until <= now())
                   AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                   AND (
                        quota_reset_at <= now()
                        OR daily_uploaded_bytes < daily_quota_bytes
                   )
                 ORDER BY last_used_at ASC NULLS FIRST
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
            )
            UPDATE accounts AS a
               SET lease_expires_at = now() + ($2 * interval '1 second')
              FROM candidate
             WHERE a.id = candidate.id
            RETURNING a.id
            """,
            AccountStatus.ACTIVE.value,
            self._lease_seconds,
        )
        if row is None:
            raise RuntimeError("no active accounts available for scheduling")

        account_id: uuid.UUID = row["id"]
        logger.info("scheduled account %s (LRU)", account_id)
        return str(account_id)

    async def mark_used(self, account_id: str) -> None:
        """Update last_used_at for the given account.

        Args:
            account_id: UUID string of the account to mark.
        """
        await self._pool.execute(
            """
            UPDATE accounts
               SET last_used_at = now(),
                   lease_expires_at = NULL
             WHERE id = $1
            """,
            uuid.UUID(account_id),
        )

    async def active_count(self) -> int:
        """Return the number of active accounts."""
        val = await self._pool.fetchval(
            "SELECT count(*) FROM accounts WHERE status = $1",
            AccountStatus.ACTIVE.value,
        )
        return int(val)
