"""Canary rejection and crash-safety contracts; these are not live Photos evidence."""

import json
import os
import sys
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


@pytest.mark.parametrize("command", ["preflight", "run", "resume", "verify", "cleanup"])
def test_retired_docker_canary_cli_never_opens_resources(command, tmp_path, monkeypatch, capsys):
    from pixav.pixel_injector import canary

    single = Mock(side_effect=AssertionError("retired entry acquired a process lock"))
    directory = Mock(side_effect=AssertionError("retired entry opened evidence"))
    connect = Mock(side_effect=AssertionError("retired entry connected to Docker"))
    monkeypatch.setattr(canary, "single_flight", single)
    monkeypatch.setattr(canary, "private_directory", directory)
    monkeypatch.setattr("docker.from_env", connect)
    evidence = tmp_path / "must-not-be-created"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "canary",
            command,
            "--source",
            str(tmp_path / "source.mp4"),
            "--profile",
            str(tmp_path / "profile.yml"),
            "--secret",
            str(tmp_path / "private.json"),
            "--evidence",
            str(evidence),
        ],
    )
    assert canary.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "RETIRED"
    assert result["reason_code"] == "LEGACY_DOCKER_CANARY_RETIRED"
    single.assert_not_called()
    directory.assert_not_called()
    connect.assert_not_called()
    assert not evidence.exists()


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


@pytest.mark.parametrize("status", ["PREFLIGHT_READY", "CREATING_RUNTIME", "REVIEW_REQUIRED", "USER_ACTION_REQUIRED"])
def test_retired_runtime_preserves_original_checkpoint_and_never_touches_docker(status):
    client, save = Mock(), Mock()
    state = {"owner": OWNER, "status": status, "login_stage": "GOOGLE_DEVICE_CHALLENGE"}
    before = state.copy()
    with pytest.raises(ValueError, match="LEGACY_DOCKER_CANARY_RETIRED"):
        prepare_runtime(client, state, "config/android_profiles.yml", save)
    assert state == before
    assert client.mock_calls == []
    save.assert_not_called()


def test_retired_source_probe_and_preflight_never_read_paths_or_create_containers(tmp_path):
    from pixav.pixel_injector.canary import preflight, probe_source

    client, source, args, checkpoint = Mock(), Mock(), Mock(), tmp_path / "checkpoint.json"
    with pytest.raises(ValueError, match="LEGACY_DOCKER_CANARY_RETIRED"):
        probe_source(client, source, "sha256:unused")
    with pytest.raises(ValueError, match="LEGACY_DOCKER_CANARY_RETIRED"):
        preflight(client, args, checkpoint)
    assert client.mock_calls == source.mock_calls == args.mock_calls == []
    assert not checkpoint.exists()


def test_private_fifo_does_not_block(tmp_path):
    fifo = tmp_path / "secret"
    os.mkfifo(fifo, mode=0o600)
    with pytest.raises(ValueError):
        read_private(fifo)


@pytest.mark.parametrize("verb", ["run", "cleanup"])
def test_retired_module_entry_leaves_work_and_private_input_untouched(tmp_path, verb):
    import subprocess

    private = tmp_path / "secret.json"
    private.write_text('{"synthetic":"must-not-be-read"}')
    os.chmod(private, 0o600)
    evidence = tmp_path / "evidence"
    before = private.stat()
    result = subprocess.run(  # noqa: S603 - fixed Python module and synthetic fixture argv; no shell
        [
            sys.executable,
            "-m",
            "pixav.pixel_injector.canary",
            verb,
            "--source",
            str(tmp_path / "source.mp4"),
            "--profile",
            str(tmp_path / "profile.yml"),
            "--secret",
            str(private),
            "--evidence",
            str(evidence),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={**os.environ, "DOCKER_HOST": "unix:///pixav-retired-test-must-not-connect.sock"},
    )
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"status": "RETIRED", "reason_code": "LEGACY_DOCKER_CANARY_RETIRED"}
    assert not result.stderr and not evidence.exists()
    after = private.stat()
    assert (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_atime_ns,
    ) == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_atime_ns)
