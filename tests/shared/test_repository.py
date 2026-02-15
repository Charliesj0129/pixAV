"""Tests for VideoRepository and TaskRepository."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pixav.shared.enums import TaskState, VideoStatus
from pixav.shared.models import Task, Video
from pixav.shared.repository import (
    AccountRepository,
    TaskRepository,
    VideoRepository,
    _task_from_row,
    _video_from_row,
)


def _make_record(data: dict[str, Any]) -> MagicMock:
    """Build a mock asyncpg.Record that behaves like a dict."""
    rec = MagicMock()
    rec.__iter__ = MagicMock(return_value=iter(data.items()))
    rec.__getitem__ = MagicMock(side_effect=data.__getitem__)
    rec.get = MagicMock(side_effect=data.get)
    rec.keys = MagicMock(return_value=data.keys())

    # dict(record) must work
    class FakeRecord(dict):  # type: ignore[type-arg]
        pass

    return FakeRecord(data)


# ── Row helpers ─────────────────────────────────────────────────


def _sample_video_row() -> dict[str, Any]:
    return {
        "id": uuid.UUID("00000000-0000-0000-0000-000000000010"),
        "title": "Test Video",
        "magnet_uri": "magnet:?xt=urn:btih:abc",
        "local_path": None,
        "share_url": None,
        "cdn_url": None,
        "status": "discovered",
        "metadata_json": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": None,
    }


def _sample_task_row() -> dict[str, Any]:
    return {
        "id": uuid.UUID("00000000-0000-0000-0000-000000000100"),
        "video_id": uuid.UUID("00000000-0000-0000-0000-000000000010"),
        "account_id": None,
        "state": "pending",
        "queue_name": "pixav:crawl",
        "retries": 0,
        "max_retries": 3,
        "error_message": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": None,
    }


# ── _video_from_row / _task_from_row ────────────────────────────


class TestVideoFromRow:
    def test_basic_conversion(self) -> None:
        row = _make_record(_sample_video_row())
        video = _video_from_row(row)
        assert isinstance(video, Video)
        assert video.title == "Test Video"
        assert video.status == VideoStatus.DISCOVERED

    def test_strips_embedding_column(self) -> None:
        data = _sample_video_row()
        data["embedding"] = [0.1, 0.2, 0.3]
        row = _make_record(data)
        video = _video_from_row(row)
        # embedding is kept on the model for internal use, but excluded from default dumps.
        assert video.embedding == [0.1, 0.2, 0.3]
        assert "embedding" not in video.model_dump()

    def test_jsonb_dict_is_serialized(self) -> None:
        data = _sample_video_row()
        data["metadata_json"] = {"title": "foo"}
        row = _make_record(data)
        video = _video_from_row(row)
        assert video.metadata_json == json.dumps({"title": "foo"})


class TestTaskFromRow:
    def test_basic_conversion(self) -> None:
        row = _make_record(_sample_task_row())
        task = _task_from_row(row)
        assert isinstance(task, Task)
        assert task.state == TaskState.PENDING
        assert task.queue_name == "pixav:crawl"


# ── VideoRepository ────────────────────────────────────────────


class TestVideoRepository:
    @pytest.fixture()
    def pool(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture()
    def repo(self, pool: AsyncMock) -> VideoRepository:
        return VideoRepository(pool)

    async def test_find_by_id_returns_video(self, repo: VideoRepository, pool: AsyncMock) -> None:
        pool.fetchrow.return_value = _make_record(_sample_video_row())
        result = await repo.find_by_id(uuid.UUID("00000000-0000-0000-0000-000000000010"))
        assert result is not None
        assert result.title == "Test Video"

    async def test_find_by_id_returns_none(self, repo: VideoRepository, pool: AsyncMock) -> None:
        pool.fetchrow.return_value = None
        result = await repo.find_by_id(uuid.uuid4())
        assert result is None

    async def test_find_by_magnet_returns_video(self, repo: VideoRepository, pool: AsyncMock) -> None:
        pool.fetchrow.return_value = _make_record(_sample_video_row())
        result = await repo.find_by_magnet("magnet:?xt=urn:btih:abc")
        assert result is not None
        assert result.magnet_uri == "magnet:?xt=urn:btih:abc"

    async def test_find_by_magnet_returns_none(self, repo: VideoRepository, pool: AsyncMock) -> None:
        pool.fetchrow.return_value = None
        result = await repo.find_by_magnet("magnet:?missing")
        assert result is None

    async def test_insert_calls_fetchrow(self, repo: VideoRepository, pool: AsyncMock) -> None:
        pool.fetchrow.return_value = _make_record(_sample_video_row())
        video = Video(title="Test Video", magnet_uri="magnet:?xt=urn:btih:abc")
        result = await repo.insert(video)
        pool.fetchrow.assert_awaited_once()
        assert result.title == "Test Video"

    async def test_update_status(self, repo: VideoRepository, pool: AsyncMock) -> None:
        pool.execute.return_value = "UPDATE 1"
        await repo.update_status(uuid.uuid4(), VideoStatus.DOWNLOADING)
        pool.execute.assert_awaited_once()

    async def test_update_download_result(self, repo: VideoRepository, pool: AsyncMock) -> None:
        pool.execute.return_value = "UPDATE 1"
        await repo.update_download_result(
            uuid.uuid4(),
            local_path="/data/remuxed/video.mp4",
            metadata_json='{"found": true}',
        )
        pool.execute.assert_awaited_once()

    async def test_update_upload_result(self, repo: VideoRepository, pool: AsyncMock) -> None:
        pool.execute.return_value = "UPDATE 1"
        await repo.update_upload_result(uuid.uuid4(), share_url="https://photos.app.goo.gl/share")
        pool.execute.assert_awaited_once()

    async def test_count_by_status(self, repo: VideoRepository, pool: AsyncMock) -> None:
        pool.fetchval.return_value = 42
        result = await repo.count_by_status(VideoStatus.DISCOVERED)
        assert result == 42


# ── TaskRepository ─────────────────────────────────────────────


class TestTaskRepository:
    @pytest.fixture()
    def pool(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture()
    def repo(self, pool: AsyncMock) -> TaskRepository:
        return TaskRepository(pool)

    async def test_find_by_id_returns_task(self, repo: TaskRepository, pool: AsyncMock) -> None:
        pool.fetchrow.return_value = _make_record(_sample_task_row())
        result = await repo.find_by_id(uuid.UUID("00000000-0000-0000-0000-000000000100"))
        assert result is not None
        assert result.state == TaskState.PENDING

    async def test_find_by_id_returns_none(self, repo: TaskRepository, pool: AsyncMock) -> None:
        pool.fetchrow.return_value = None
        result = await repo.find_by_id(uuid.uuid4())
        assert result is None

    async def test_insert_calls_fetchrow(self, repo: TaskRepository, pool: AsyncMock) -> None:
        pool.fetchrow.return_value = _make_record(_sample_task_row())
        task = Task(video_id=uuid.uuid4())
        result = await repo.insert(task)
        pool.fetchrow.assert_awaited_once()
        assert result.state == TaskState.PENDING

    async def test_update_state(self, repo: TaskRepository, pool: AsyncMock) -> None:
        pool.execute.return_value = "UPDATE 1"
        await repo.update_state(uuid.uuid4(), TaskState.DOWNLOADING)
        pool.execute.assert_awaited_once()

    async def test_update_state_with_error(self, repo: TaskRepository, pool: AsyncMock) -> None:
        pool.execute.return_value = "UPDATE 1"
        await repo.update_state(uuid.uuid4(), TaskState.FAILED, error_message="boom")
        pool.execute.assert_awaited_once()
        # verify error_message was passed
        call_args = pool.execute.call_args
        assert call_args[0][3] == "boom"

    async def test_set_retry(self, repo: TaskRepository, pool: AsyncMock) -> None:
        pool.execute.return_value = "UPDATE 1"
        task_id = uuid.uuid4()
        await repo.set_retry(
            task_id,
            retries=2,
            state=TaskState.PENDING,
            error_message="transient failure",
        )
        pool.execute.assert_awaited_once()
        args = pool.execute.call_args[0]
        assert args[1] == 2
        assert args[2] == TaskState.PENDING.value
        assert args[3] == "transient failure"
        assert args[5] == task_id

    async def test_count_by_state(self, repo: TaskRepository, pool: AsyncMock) -> None:
        pool.fetchval.return_value = 7
        result = await repo.count_by_state(TaskState.PENDING)
        assert result == 7

    async def test_list_pending_returns_tasks(self, repo: TaskRepository, pool: AsyncMock) -> None:
        pool.fetch.return_value = [_make_record(_sample_task_row())]

        result = await repo.list_pending(limit=10)

        assert len(result) == 1
        assert result[0].state == TaskState.PENDING
        pool.fetch.assert_awaited_once()

    async def test_has_open_task_true(self, repo: TaskRepository, pool: AsyncMock) -> None:
        pool.fetchval.return_value = True

        result = await repo.has_open_task(uuid.uuid4())

        assert result is True
        pool.fetchval.assert_awaited_once()

    async def test_has_open_task_false(self, repo: TaskRepository, pool: AsyncMock) -> None:
        pool.fetchval.return_value = False

        result = await repo.has_open_task(uuid.uuid4())

        assert result is False


class TestAccountRepository:
    @pytest.fixture()
    def pool(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture()
    def repo(self, pool: AsyncMock) -> AccountRepository:
        return AccountRepository(pool)

    async def test_release_expired_cooldowns_returns_count(self, repo: AccountRepository, pool: AsyncMock) -> None:
        pool.execute.return_value = "UPDATE 2"

        count = await repo.release_expired_cooldowns()

        assert count == 2
        pool.execute.assert_awaited_once()

    async def test_apply_upload_usage_executes_update(self, repo: AccountRepository, pool: AsyncMock) -> None:
        pool.execute.return_value = "UPDATE 1"
        account_id = uuid.uuid4()

        await repo.apply_upload_usage(account_id, 123456)

        pool.execute.assert_awaited_once()
        args = pool.execute.call_args[0]
        assert args[1] == account_id
        assert args[2] == 123456
