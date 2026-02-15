"""Tests for Hybrid Search functionality in VideoRepository."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from pixav.shared.models import Video
from pixav.shared.repository import VideoRepository


def _make_record(data: dict) -> dict:
    # asyncpg.Record behaves like a mapping; using a dict is sufficient for these unit tests.
    return dict(data)


class TestHybridSearch:
    @pytest.fixture()
    def pool(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture()
    def repo(self, pool: AsyncMock) -> VideoRepository:
        return VideoRepository(pool)

    async def test_update_embedding(self, repo: VideoRepository, pool: AsyncMock) -> None:
        video_id = uuid.uuid4()
        embedding = [0.1, 0.2, 0.3]
        pool.execute.return_value = "UPDATE 1"

        await repo.update_embedding(video_id, embedding)

        pool.execute.assert_awaited_once()
        args = pool.execute.call_args[0]
        assert args[0] == "UPDATE videos SET embedding = $1 WHERE id = $2"
        assert args[1] == embedding
        assert args[2] == video_id

    async def test_find_missing_embeddings(self, repo: VideoRepository, pool: AsyncMock) -> None:
        row_data = {
            "id": uuid.uuid4(),
            "title": "Test",
            "magnet_uri": "magnet:?foo",
            "status": "discovered",
            "created_at": datetime.now(timezone.utc),
            "tags": [],
            "files": [],
        }
        pool.fetch.return_value = [_make_record(row_data)]

        results = await repo.find_missing_embeddings(limit=50)

        pool.fetch.assert_awaited_once()
        args = pool.fetch.call_args[0]
        assert "WHERE embedding IS NULL" in args[0]
        assert args[1] == 50
        assert len(results) == 1
        assert isinstance(results[0], Video)

    async def test_search_calls_correct_sql(self, repo: VideoRepository, pool: AsyncMock) -> None:
        row_data = {
            "id": uuid.uuid4(),
            "title": "Match",
            "magnet_uri": "magnet:?foo",
            "status": "available",
            "created_at": datetime.now(timezone.utc),
            "tags": [],
            "embedding": [0.1],  # mock return
        }
        pool.fetch.return_value = [_make_record(row_data)]

        query = "office lady"
        embedding = [0.1, 0.2, 0.3]

        results = await repo.search(query, embedding, limit=10)
        assert len(results) == 1
        assert isinstance(results[0], Video)

        pool.fetch.assert_awaited_once()
        sql = pool.fetch.call_args[0][0]

        # Verify Key Logic
        assert "WITH semantic AS" in sql
        assert "keyword AS" in sql
        assert "embedding <=> $2" in sql  # vector distance
        assert "websearch_to_tsquery('simple', $1)" in sql  # lexical search
        assert "rrf_score" in sql  # Reciprocal Rank Fusion
        assert "ORDER BY rrf_score DESC" in sql

        # Verify Args
        args = pool.fetch.call_args[0]
        assert args[1] == query
        assert args[2] == embedding
        assert args[3] == 10
