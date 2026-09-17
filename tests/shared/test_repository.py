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
    SourceCandidateRepository,
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


def _transactional_pool(row: dict[str, Any] | None) -> tuple[AsyncMock, AsyncMock]:
    """Build a pool whose ``acquire()``/``transaction()`` behave as async context managers."""
    conn = AsyncMock()
    conn.fetchrow.return_value = row
    conn.transaction = MagicMock(return_value=_async_ctx(None))
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=_async_ctx(conn))
    return conn, pool


def _async_ctx(value: Any) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=value)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


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
        "local_path": None,
        "share_url": None,
        "retries": 0,
        "max_retries": 3,
        "error_message": None,
        "trace_id": "trace-task-row-001",
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
        assert task.trace_id == "trace-task-row-001"


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

    async def test_update_download_result_merges_metadata_instead_of_replacing(
        self, repo: VideoRepository, pool: AsyncMock
    ) -> None:
        """Later enrichment must not erase the discovery provenance written at crawl time."""
        pool.execute.return_value = "UPDATE 1"
        await repo.update_download_result(
            uuid.uuid4(),
            local_path="/data/remuxed/video.mp4",
            metadata_json='{"media": {"codec": "h264"}}',
            title="SSIS-123",
            quality_score=70,
        )
        sql = pool.execute.call_args[0][0]
        assert "metadata_json = COALESCE(metadata_json, '{}'::jsonb) || COALESCE($2::jsonb, '{}'::jsonb)" in sql
        # A blank incoming title must never blank out an existing one.
        assert "title = COALESCE(NULLIF(btrim($3), ''), title)" in sql
        assert "quality_score = COALESCE($4, quality_score)" in sql

    async def test_update_metadata_section_targets_one_key(self, repo: VideoRepository, pool: AsyncMock) -> None:
        pool.execute.return_value = "UPDATE 1"
        await repo.update_metadata_section(uuid.uuid4(), "stash", {"stash_id": "abc"})
        sql, section, value, _video_id = pool.execute.call_args[0]
        assert "jsonb_set(" in sql
        assert section == "stash"
        assert json.loads(value) == {"stash_id": "abc"}

    async def test_upload_result_sets_cleanup_window_only_for_remote_uploads(
        self, repo: VideoRepository, pool: AsyncMock
    ) -> None:
        """local mode keeps its file forever so /local/{video_id} keeps working."""
        pool.execute.return_value = "UPDATE 1"
        await repo.update_upload_result(uuid.uuid4(), share_url="https://photos.app.goo.gl/share")
        sql = pool.execute.call_args[0][0]
        assert "WHEN $1 LIKE 'pixav-local://%' THEN NULL" in sql
        # The cast is required by real PostgreSQL: without it, the parameter is
        # inferred as interval and the CASE result cannot be stored as timestamptz.
        assert "$3::timestamptz + interval '24 hours'" in sql

    async def test_schedule_terminal_cleanup_retains_failed_files(self, repo: VideoRepository, pool: AsyncMock) -> None:
        pool.execute.return_value = "UPDATE 1"
        await repo.schedule_terminal_cleanup(uuid.uuid4(), retention_days=7)
        sql, days, _video_id = pool.execute.call_args[0]
        assert days == 7
        assert "now() + ($1 * interval '1 day')" in sql
        assert "share_url LIKE 'pixav-local://%' THEN NULL" in sql

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

    async def test_insert_persists_runtime_fields_and_trace_id(self, repo: TaskRepository, pool: AsyncMock) -> None:
        pool.fetchrow.return_value = _make_record(_sample_task_row())
        task = Task(
            video_id=uuid.uuid4(),
            local_path="/tmp/video.mp4",
            share_url="https://photos.app.goo.gl/example",
            trace_id="trace-insert-001",
        )

        await repo.insert(task)

        args = pool.fetchrow.call_args[0]
        sql = args[0]
        assert "local_path" in sql
        assert "share_url" in sql
        assert "trace_id" in sql
        assert args[6] == "/tmp/video.mp4"
        assert args[7] == "https://photos.app.goo.gl/example"
        assert args[11] == "trace-insert-001"

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
        assert args[4] is None
        assert args[6] == task_id

    async def test_claim_for_dispatch_true(self, repo: TaskRepository, pool: AsyncMock) -> None:
        pool.execute.return_value = "UPDATE 1"
        task_id = uuid.uuid4()
        account_id = uuid.uuid4()

        claimed = await repo.claim_for_dispatch(
            task_id,
            next_state=TaskState.DOWNLOADING,
            account_id=account_id,
        )

        assert claimed is True
        pool.execute.assert_awaited_once()

    async def test_claim_for_dispatch_false(self, repo: TaskRepository, pool: AsyncMock) -> None:
        pool.execute.return_value = "UPDATE 0"

        claimed = await repo.claim_for_dispatch(
            uuid.uuid4(),
            next_state=TaskState.UPLOADING,
        )

        assert claimed is False

    async def test_release_dispatch_claim(self, repo: TaskRepository, pool: AsyncMock) -> None:
        task_id = uuid.uuid4()
        await repo.release_dispatch_claim(task_id, error_message="dispatch failed", clear_account=True)
        pool.execute.assert_awaited_once()
        args = pool.execute.call_args[0]
        assert args[1] == TaskState.PENDING.value
        assert args[2] is True
        assert args[3] == "dispatch failed"
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

    async def test_list_pending_skips_tasks_still_in_backoff(self, repo: TaskRepository, pool: AsyncMock) -> None:
        """Maxwell only dispatches retries whose PostgreSQL due time has passed."""
        pool.fetch.return_value = []

        await repo.list_pending(limit=10)

        sql = pool.fetch.call_args[0][0]
        assert "retry_not_before IS NULL OR retry_not_before <= now()" in sql

    async def test_claim_for_dispatch_refuses_tasks_still_in_backoff(
        self, repo: TaskRepository, pool: AsyncMock
    ) -> None:
        pool.execute.return_value = "UPDATE 0"

        await repo.claim_for_dispatch(uuid.uuid4(), next_state=TaskState.DISPATCHED)

        sql = pool.execute.call_args[0][0]
        assert "retry_not_before IS NULL OR retry_not_before <= now()" in sql

    async def test_set_retry_persists_the_due_time(self, repo: TaskRepository, pool: AsyncMock) -> None:
        pool.execute.return_value = "UPDATE 1"
        due = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

        await repo.set_retry(uuid.uuid4(), 3, error_message="boom", retry_not_before=due)

        sql = pool.execute.call_args[0][0]
        assert "retry_not_before = $4" in sql
        assert pool.execute.call_args[0][4] == due

    async def test_replay_audits_and_restarts_the_retry_cycle(self) -> None:
        conn, pool = _transactional_pool({"state": "failed", "retries": 6})
        task_id = uuid.uuid4()

        assert await TaskRepository(pool).replay(task_id, requested_by="operator", reason="ADB fixed") is True

        audit_sql, audited_id, prev_state, prev_retries, by, reason = conn.execute.await_args_list[0][0]
        assert "INSERT INTO task_replay_audit" in audit_sql
        assert (audited_id, prev_state, prev_retries, by, reason) == (
            task_id,
            "failed",
            6,
            "operator",
            "ADB fixed",
        )
        update_sql = conn.execute.await_args_list[1][0][0]
        assert "state = 'pending', retries = 0, retry_not_before = now()" in update_sql

    async def test_replay_returns_false_for_unknown_task(self) -> None:
        conn, pool = _transactional_pool(None)

        assert await TaskRepository(pool).replay(uuid.uuid4()) is False
        conn.execute.assert_not_awaited()

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


class TestSourceCandidateRepository:
    """Cooldown, not deletion: a swarm dead today may be alive next week."""

    @pytest.fixture
    def pool(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def repo(self, pool: AsyncMock) -> SourceCandidateRepository:
        return SourceCandidateRepository(pool)

    async def test_register_is_idempotent_per_video_and_magnet(
        self, repo: SourceCandidateRepository, pool: AsyncMock
    ) -> None:
        await repo.register(uuid.uuid4(), magnet_uri="magnet:?xt=urn:btih:abc", quality_score=42)

        query = pool.execute.await_args.args[0]
        assert "ON CONFLICT (video_id, magnet_uri) DO NOTHING" in query

    async def test_mark_unavailable_parks_the_row_instead_of_deleting_it(
        self, repo: SourceCandidateRepository, pool: AsyncMock
    ) -> None:
        await repo.mark_unavailable(uuid.uuid4(), "magnet:?xt=urn:btih:abc", reason="no seeds", cooldown_hours=6)

        query = pool.execute.await_args.args[0]
        assert "DELETE" not in query.upper()
        assert "unavailable_until" in query
        assert "attempts = attempts + 1" in query

    async def test_mark_unavailable_truncates_the_error_text(
        self, repo: SourceCandidateRepository, pool: AsyncMock
    ) -> None:
        await repo.mark_unavailable(uuid.uuid4(), "magnet:x", reason="e" * 2000)

        assert len(pool.execute.await_args.args[4]) == 500

    async def test_next_candidate_excludes_the_failed_source(
        self, repo: SourceCandidateRepository, pool: AsyncMock
    ) -> None:
        video_id = uuid.uuid4()
        pool.fetchrow.return_value = {
            "id": uuid.uuid4(),
            "video_id": video_id,
            "magnet_uri": "magnet:?xt=urn:btih:def",
            "info_hash": "def",
            "origin": "sehuatang",
            "quality_score": 10,
            "state": "pending",
            "unavailable_until": None,
            "attempts": 0,
            "last_error": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": None,
        }

        result = await repo.next_candidate(video_id, exclude_magnet="magnet:?xt=urn:btih:abc")

        assert result is not None
        assert result.magnet_uri == "magnet:?xt=urn:btih:def"
        assert pool.fetchrow.await_args.args[2] == "magnet:?xt=urn:btih:abc"

    async def test_next_candidate_returns_none_when_exhausted(
        self, repo: SourceCandidateRepository, pool: AsyncMock
    ) -> None:
        pool.fetchrow.return_value = None

        assert await repo.next_candidate(uuid.uuid4()) is None

    async def test_release_expired_cooldowns_reports_the_row_count(
        self, repo: SourceCandidateRepository, pool: AsyncMock
    ) -> None:
        pool.execute.return_value = "UPDATE 3"

        assert await repo.release_expired_cooldowns() == 3
