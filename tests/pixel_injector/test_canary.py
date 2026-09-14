"""Canary rejection and crash-safety contracts; these are not live Photos evidence."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pixav.pixel_injector.canary import (
    OWNER_LABEL,
    Media,
    Observation,
    evaluate,
    owned_containers,
    prepare_runtime,
    private_directory,
    read_private,
    single_flight,
    write_private,
)

OWNER = "7880fc5a-e3bf-4c0b-beb6-72f03b8b6c2e"


@pytest.fixture
def media():
    return Media(size=128742951, sha256="a" * 64, width=3840, height=2160, codec="h264", duration=100.07)


@pytest.fixture
def observation():
    return Observation(
        source_sha256="a" * 64,
        filename="canary.mp4",
        backed_up=True,
        original_quality=True,
        uploaded_at=100,
        before_at=99,
        after_at=86500,
        before_bytes=1000,
        after_bytes=1000,
        precision_bytes=10,
        item_charged_bytes=0,
        independent_browser_download=True,
        decode_passed=True,
    )


def test_complete_measurements(media, observation):
    assert evaluate(media, media, observation, "canary.mp4") == {"upload": "PASS", "original": "PASS", "quota": "PASS"}


@pytest.mark.parametrize(
    "change",
    [
        {"width": 1920, "height": 1080},
        {"width": 1280, "height": 720},
        {"sha256": "b" * 64},
        {"size": 128742950},
    ],
)
def test_rendition_truncation_and_hash_fail(media, observation, change):
    assert evaluate(media, media.model_copy(update=change), observation, "canary.mp4")["original"] == "FAIL"


def test_1080p_source_cannot_claim_4k(media, observation):
    source = media.model_copy(update={"width": 1920, "height": 1080})
    assert evaluate(source, source, observation, "canary.mp4")["original"] == "FAIL"


@pytest.mark.parametrize(
    "change",
    [
        {"filename": "other.mp4"},
        {"source_sha256": "b" * 64},
        {"backed_up": False},
        {"original_quality": False},
    ],
)
def test_wrong_or_unbacked_item(media, observation, change):
    result = evaluate(media, media, observation.model_copy(update=change), "canary.mp4")
    assert result["upload"] == result["original"] == "FAIL"


@pytest.mark.parametrize(
    "change",
    [
        {"after_at": 86499},
        {"item_charged_bytes": None},
        {"precision_bytes": 128742951},
        {"before_at": 101},
    ],
)
def test_insufficient_quota_stays_open(media, observation, change):
    assert evaluate(media, media, observation.model_copy(update=change), "canary.mp4")["quota"] == "OPEN"


@pytest.mark.parametrize("change", [{"after_bytes": 1001}, {"item_charged_bytes": 1}])
def test_quota_increase_fails(media, observation, change):
    assert evaluate(media, media, observation.model_copy(update=change), "canary.mp4")["quota"] == "FAIL"


@pytest.mark.parametrize("change", [{"decode_passed": False}, {"independent_browser_download": False}])
def test_local_copy_is_not_cloud_proof(media, observation, change):
    assert evaluate(media, media, observation.model_copy(update=change), "canary.mp4")["original"] == "FAIL"


def test_private_state_symlink_and_permissions(tmp_path):
    root = private_directory(tmp_path / "private")
    target = root / "checkpoint.json"
    write_private(target, {"status": "CREATING_RUNTIME"})
    assert read_private(target)["status"] == "CREATING_RUNTIME"
    assert target.stat().st_mode & 0o777 == 0o600
    os.chmod(target, 0o644)
    with pytest.raises(ValueError):
        read_private(target)
    link = root / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        write_private(link, {})
    with pytest.raises(OSError):
        read_private(link)
    with pytest.raises(ValueError):
        private_directory(link)


def test_single_flight_across_runs(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    with single_flight():
        with pytest.raises(BlockingIOError):
            with single_flight():
                pytest.fail("second runner acquired lock")
    with single_flight():
        pass


def test_ownership_rejects_production_label():
    client = Mock()
    client.containers.list.return_value = [SimpleNamespace(labels={OWNER_LABEL: OWNER, "pixav.task_id": "x"})]
    with pytest.raises(ValueError):
        owned_containers(client, OWNER)
    client.containers.list.assert_called_once_with(all=True, filters={"label": f"{OWNER_LABEL}={OWNER}"})


def test_resume_never_recreates_uncertain_guest():
    client = Mock()
    client.containers.list.return_value = [SimpleNamespace(labels={OWNER_LABEL: OWNER})]
    state = {"owner": OWNER, "status": "CREATING_RUNTIME"}
    save = Mock()
    prepare_runtime(client, state, "config/android_profiles.yml", save)
    assert state["status"] == "REVIEW_REQUIRED"
    client.containers.run.assert_not_called()
    save.assert_called_once()


def test_intent_persisted_before_create_failure():
    client = Mock()
    client.containers.list.return_value = []
    client.containers.run.side_effect = RuntimeError("interrupted")
    state = {"owner": OWNER, "status": "PREFLIGHT_READY"}
    snapshots = []
    with pytest.raises(RuntimeError):
        prepare_runtime(client, state, "config/android_profiles.yml", lambda: snapshots.append(state.copy()))
    assert snapshots[0]["status"] == "CREATING_RUNTIME"
    kwargs = client.containers.run.call_args.kwargs
    assert "pixav.task_id" not in kwargs["labels"]
    assert kwargs["ports"] == {"5555/tcp": ("127.0.0.1", None)}
    with pytest.raises(ValueError):
        prepare_runtime(client, state, "config/android_profiles.yml", Mock())
    assert client.containers.run.call_count == 1


def test_private_fifo_does_not_block(tmp_path):
    fifo = tmp_path / "secret"
    os.mkfifo(fifo, mode=0o600)
    with pytest.raises(ValueError):
        read_private(fifo)


def test_owned_runtime_pair_uses_pinned_tools_and_guest_network():
    client = Mock()
    client.containers.list.return_value = []
    client.containers.run.side_effect = [SimpleNamespace(id="guest-id"), SimpleNamespace(id="runner-id")]
    state = {"owner": OWNER, "status": "PREFLIGHT_READY", "tools_image": "sha256:pinned"}
    prepare_runtime(client, state, "config/android_profiles.yml", Mock())
    assert state["status"] == "BLOCKED_UI_CALIBRATION"
    assert state["runner_id"] == "runner-id"
    second = client.containers.run.call_args_list[1]
    assert second.args == ("sha256:pinned",)
    assert second.kwargs["network_mode"] == "container:guest-id"
    assert second.kwargs["labels"][OWNER_LABEL] == OWNER


def test_resume_preserves_google_challenge_without_login_retry():
    client = Mock()
    client.containers.list.return_value = [SimpleNamespace(labels={OWNER_LABEL: OWNER})]
    state = {"owner": OWNER, "status": "USER_ACTION_REQUIRED", "login_stage": "GOOGLE_DEVICE_CHALLENGE"}
    prepare_runtime(client, state, "config/android_profiles.yml", Mock())
    assert state["status"] == "USER_ACTION_REQUIRED"
    client.containers.run.assert_not_called()
