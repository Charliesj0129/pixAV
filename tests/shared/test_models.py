"""Tests for frozen Pydantic domain models."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from pixav.shared.enums import AccountStatus, TaskState, VideoStatus
from pixav.shared.models import Account, StorageInstance, Task, Video


class TestAccount:
    def test_create_with_defaults(self) -> None:
        acct = Account(email="a@b.com")
        assert acct.email == "a@b.com"
        assert acct.status == AccountStatus.ACTIVE
        assert acct.storage_instance_id is None
        assert isinstance(acct.id, uuid.UUID)

    def test_frozen_raises_on_mutation(self) -> None:
        acct = Account(email="a@b.com")
        with pytest.raises(ValidationError):
            acct.email = "changed@b.com"  # type: ignore[misc]

    def test_model_copy_returns_new_instance(self) -> None:
        acct = Account(email="a@b.com")
        updated = acct.model_copy(update={"status": AccountStatus.COOLDOWN})
        assert updated.status == AccountStatus.COOLDOWN
        assert acct.status == AccountStatus.ACTIVE


class TestVideo:
    def test_create_with_defaults(self) -> None:
        vid = Video(title="My Video")
        assert vid.title == "My Video"
        assert vid.status == VideoStatus.DISCOVERED
        assert vid.magnet_uri is None

    def test_frozen(self) -> None:
        vid = Video(title="X")
        with pytest.raises(ValidationError):
            vid.title = "Y"  # type: ignore[misc]


class TestTask:
    def test_create_with_video_id(self) -> None:
        vid_id = uuid.uuid4()
        task = Task(video_id=vid_id)
        assert task.video_id == vid_id
        assert task.state == TaskState.PENDING
        assert task.retries == 0
        assert task.max_retries == 3

    def test_immutable_update(self) -> None:
        vid_id = uuid.uuid4()
        task = Task(video_id=vid_id)
        updated = task.model_copy(update={"state": TaskState.UPLOADING, "retries": 1})
        assert updated.state == TaskState.UPLOADING
        assert updated.retries == 1
        assert task.state == TaskState.PENDING


class TestStorageInstance:
    def test_create_with_defaults(self) -> None:
        acct_id = uuid.uuid4()
        si = StorageInstance(account_id=acct_id)
        assert si.account_id == acct_id
        assert si.capacity_bytes == 0
        assert si.used_bytes == 0
