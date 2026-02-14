"""Tests for shared enum definitions."""

from __future__ import annotations

from pixav.shared.enums import AccountStatus, StorageHealth, TaskState, VideoStatus


class TestTaskState:
    def test_all_states_present(self) -> None:
        expected = {"pending", "downloading", "remuxing", "uploading", "verifying", "complete", "failed"}
        assert {s.value for s in TaskState} == expected

    def test_string_value(self) -> None:
        assert TaskState.PENDING == "pending"
        assert TaskState.COMPLETE == "complete"


class TestAccountStatus:
    def test_all_statuses_present(self) -> None:
        expected = {"active", "cooldown", "banned", "unverified"}
        assert {s.value for s in AccountStatus} == expected


class TestVideoStatus:
    def test_all_statuses_present(self) -> None:
        expected = {"discovered", "downloading", "downloaded", "uploading", "available", "expired", "failed"}
        assert {s.value for s in VideoStatus} == expected


class TestStorageHealth:
    def test_all_states_present(self) -> None:
        expected = {"healthy", "degraded", "full", "offline"}
        assert {s.value for s in StorageHealth} == expected
