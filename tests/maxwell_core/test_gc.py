"""Tests for OrphanTaskCleaner."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from pixav.maxwell_core.gc import OrphanTaskCleaner, _parse_update_count


@pytest.fixture
def mock_pool() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def cleaner(mock_pool: AsyncMock) -> OrphanTaskCleaner:
    return OrphanTaskCleaner(mock_pool, max_age=timedelta(hours=1))


class TestOrphanTaskCleaner:
    async def test_cleanup_finds_orphans(self, cleaner: OrphanTaskCleaner, mock_pool: AsyncMock) -> None:
        mock_pool.execute.return_value = "UPDATE 3"

        count = await cleaner.cleanup()

        assert count == 3
        mock_pool.execute.assert_awaited_once()
        query = mock_pool.execute.call_args[0][0]
        assert "state = $1" in query
        assert "updated_at < now()" in query

    async def test_cleanup_no_orphans(self, cleaner: OrphanTaskCleaner, mock_pool: AsyncMock) -> None:
        mock_pool.execute.return_value = "UPDATE 0"

        count = await cleaner.cleanup()
        assert count == 0

    async def test_cleanup_expired_videos(self, cleaner: OrphanTaskCleaner, mock_pool: AsyncMock) -> None:
        mock_pool.execute.return_value = "UPDATE 5"

        count = await cleaner.cleanup_expired_videos()
        assert count == 5


class TestParseUpdateCount:
    def test_parses_update_n(self) -> None:
        assert _parse_update_count("UPDATE 42") == 42

    def test_parses_zero(self) -> None:
        assert _parse_update_count("UPDATE 0") == 0

    def test_invalid_returns_zero(self) -> None:
        assert _parse_update_count("") == 0
        assert _parse_update_count("INSERT 1") == 1

    def test_unparseable_returns_zero(self) -> None:
        assert _parse_update_count("ERROR") == 0
