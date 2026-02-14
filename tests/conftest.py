"""Shared pytest fixtures for the pixAV test suite."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from pixav.config import Settings
from pixav.shared.enums import AccountStatus, TaskState, VideoStatus
from pixav.shared.models import Account, Task, Video


@pytest.fixture()
def settings() -> Settings:
    """Return a Settings instance with test defaults."""
    return Settings(
        db_host="localhost",
        db_port=5432,
        db_user="test",
        db_password="test",
        db_name="pixav_test",
        redis_url="redis://localhost:6379/1",
    )


@pytest.fixture()
def sample_account() -> Account:
    return Account(
        id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        email="test@gmail.com",
        status=AccountStatus.ACTIVE,
    )


@pytest.fixture()
def sample_video() -> Video:
    return Video(
        id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        title="Test Video",
        status=VideoStatus.DISCOVERED,
    )


@pytest.fixture()
def sample_task(sample_video: Video, sample_account: Account) -> Task:
    return Task(
        id=uuid.UUID("00000000-0000-0000-0000-000000000100"),
        video_id=sample_video.id,
        account_id=sample_account.id,
        state=TaskState.PENDING,
        queue_name="pixav:upload",
        local_path="/tmp/test.mp4",
    )


@pytest.fixture()
def mock_redis() -> AsyncMock:
    """Mock async Redis client."""
    mock = AsyncMock()
    mock.rpush = AsyncMock(return_value=1)
    mock.blpop = AsyncMock(return_value=None)
    mock.llen = AsyncMock(return_value=0)
    mock.get = AsyncMock(return_value=None)
    mock.setex = AsyncMock()
    return mock
