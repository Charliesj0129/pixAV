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

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def next_account(self) -> str:
        """Return the account ID least recently used for uploading.

        Returns:
            Account UUID string.

        Raises:
            RuntimeError: If no active accounts are available.
        """
        row = await self._pool.fetchrow(
            """
            SELECT id FROM accounts
             WHERE status = $1
             ORDER BY last_used_at ASC NULLS FIRST
             LIMIT 1
            """,
            AccountStatus.ACTIVE.value,
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
            "UPDATE accounts SET last_used_at = now() WHERE id = $1",
            uuid.UUID(account_id),
        )

    async def active_count(self) -> int:
        """Return the number of active accounts."""
        val = await self._pool.fetchval(
            "SELECT count(*) FROM accounts WHERE status = $1",
            AccountStatus.ACTIVE.value,
        )
        return int(val)
