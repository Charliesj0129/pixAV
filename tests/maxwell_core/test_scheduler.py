"""Tests for LruAccountScheduler."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from pixav.maxwell_core.scheduler import LruAccountScheduler
from pixav.shared.enums import AccountStatus


@pytest.fixture
def mock_pool() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def scheduler(mock_pool: AsyncMock) -> LruAccountScheduler:
    return LruAccountScheduler(mock_pool)


class TestLruAccountScheduler:
    async def test_next_account_returns_lru(self, scheduler: LruAccountScheduler, mock_pool: AsyncMock) -> None:
        acct_id = uuid.uuid4()
        mock_pool.fetchrow.return_value = {"id": acct_id}

        result = await scheduler.next_account()

        assert result == str(acct_id)
        mock_pool.fetchrow.assert_awaited_once()
        call_args = mock_pool.fetchrow.call_args
        # Verify the query uses ACTIVE status and ORDER BY last_used_at ASC
        query = call_args[0][0]
        assert "ORDER BY last_used_at ASC" in query
        assert call_args[0][1] == AccountStatus.ACTIVE.value
        assert mock_pool.execute.await_count >= 1

    async def test_next_account_no_active_raises(self, scheduler: LruAccountScheduler, mock_pool: AsyncMock) -> None:
        mock_pool.fetchrow.return_value = None

        with pytest.raises(RuntimeError, match="no active accounts"):
            await scheduler.next_account()

    async def test_mark_used(self, scheduler: LruAccountScheduler, mock_pool: AsyncMock) -> None:
        acct_id = str(uuid.uuid4())
        mock_pool.execute.return_value = "UPDATE 1"

        await scheduler.mark_used(acct_id)

        mock_pool.execute.assert_awaited_once()
        call_args = mock_pool.execute.call_args
        assert "last_used_at" in call_args[0][0]
        assert call_args[0][1] == uuid.UUID(acct_id)

    async def test_active_count(self, scheduler: LruAccountScheduler, mock_pool: AsyncMock) -> None:
        mock_pool.fetchval.return_value = 3

        result = await scheduler.active_count()

        assert result == 3
        mock_pool.fetchval.assert_awaited_once()
