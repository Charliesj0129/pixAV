"""A paused torrent or stale Docker name never proves process quiescence."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pixav.maxwell_core.cleanup_quiescence import StoppedDownloaderGuard, capture_downloader_mount
from pixav.shared.cleanup_recovery import CleanupApprovalError, CleanupRuntimeBinding, EvidenceFile

RUNTIME = CleanupRuntimeBinding(docker_context="synthetic", docker_daemon_id="synthetic-daemon", redis_run_id="a" * 40)


@pytest.fixture
def downloader(tmp_path):
    tmp_path.chmod(0o700)
    attributes = {
        "Id": "a" * 64,
        "Image": "sha256:" + "b" * 64,
        "Created": "2026-01-01T00:00:00Z",
        "Config": {"Labels": {"com.docker.compose.project": "synthetic", "com.docker.compose.service": "qbittorrent"}},
        "State": {
            "Status": "running",
            "Running": True,
            "Paused": False,
            "Restarting": False,
            "Dead": False,
            "Pid": 123,
            "StartedAt": "2026-01-01T00:01:00Z",
        },
        "HostConfig": {"RestartPolicy": {"Name": "no"}},
        "Mounts": [{"Type": "bind", "Source": str(tmp_path), "Destination": "/downloads", "RW": True}],
    }
    container = Mock(id=attributes["Id"], attrs=attributes)
    container.exec_run.return_value = SimpleNamespace(
        exit_code=0, output=f"{tmp_path.stat().st_dev} {tmp_path.stat().st_ino}\n".encode()
    )
    client = Mock()
    client.info.return_value = {"ID": RUNTIME.docker_daemon_id}
    client.containers.get.return_value = container
    client.containers.list.return_value = [container]
    binding = capture_downloader_mount(client, container.id, "/downloads", tmp_path, RUNTIME)
    path = tmp_path / "mount.json"
    path.write_bytes(binding.model_dump_json().encode())
    path.chmod(0o600)
    reference = EvidenceFile(
        path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size_bytes=path.stat().st_size
    )
    attributes["State"].update(Status="exited", Running=False, Pid=0)
    maintenance = SimpleNamespace(check=AsyncMock(return_value=RUNTIME))
    guard = StoppedDownloaderGuard(client, maintenance, reference, tmp_path)
    return guard, client, container, binding, tmp_path


async def test_downloader_requires_exact_stopped_epoch_and_a_previously_measured_mount(downloader):
    guard, client, container, _, _ = downloader
    assert await guard.check() == RUNTIME
    assert guard.maintenance.check.await_count == 2
    container.exec_run.assert_called_once_with(["stat", "-c", "%d %i", "/downloads"])
    client.containers.run.assert_not_called()
    container.start.assert_not_called()
    container.stop.assert_not_called()
    container.remove.assert_not_called()


@pytest.mark.parametrize(
    "fault",
    [
        "running",
        "paused",
        "pid",
        "restart",
        "new-epoch",
        "new-image",
        "duplicate",
        "missing",
        "root",
        "receipt",
        "runtime",
    ],
)
async def test_downloader_refuses_unconfirmed_or_changed_writer_identity(downloader, fault):
    guard, client, container, _, root = downloader
    attributes = container.attrs
    if fault in {"running", "paused", "pid"}:
        attributes["State"].update(
            {
                "running": {"Status": "running", "Running": True, "Pid": 123},
                "paused": {"Paused": True},
                "pid": {"Pid": 123},
            }[fault]
        )
    elif fault == "restart":
        attributes["HostConfig"]["RestartPolicy"]["Name"] = "unless-stopped"
    elif fault == "new-epoch":
        attributes["State"]["StartedAt"] = "2026-01-01T00:02:00Z"
    elif fault == "new-image":
        attributes["Image"] = "sha256:" + "c" * 64
    elif fault == "duplicate":
        client.containers.list.return_value = [container, container]
    elif fault == "missing":
        client.containers.list.return_value = []
    elif fault == "root":
        other = root / "other"
        other.mkdir()
        attributes["Mounts"][0]["Source"] = str(other)
    elif fault == "receipt":
        (root / "mount.json").write_text(json.dumps({"unrelated": True}))
    else:
        guard.maintenance.check.return_value = RUNTIME.model_copy(update={"docker_context": "unrelated-context"})
    with pytest.raises(CleanupApprovalError):
        await guard.check()


def test_mount_capture_refuses_a_different_actual_inode(downloader):
    _, client, container, _, root = downloader
    container.attrs["State"].update(Status="running", Running=True, Pid=123)
    container.exec_run.return_value.output = b"1 2\n"
    with pytest.raises(CleanupApprovalError, match="MOUNT_MISMATCH"):
        capture_downloader_mount(client, container.id, "/downloads", root, RUNTIME)


def test_mount_capture_detects_restart_even_if_the_sdk_mapping_is_mutated_in_place(downloader):
    _, client, container, _, root = downloader
    container.attrs["State"].update(Status="running", Running=True, Pid=123)

    def replaced(*args):
        container.attrs["State"]["StartedAt"] = "2026-01-01T00:03:00Z"
        return SimpleNamespace(exit_code=0, output=f"{root.stat().st_dev} {root.stat().st_ino}\n".encode())

    container.exec_run.side_effect = replaced
    with pytest.raises(CleanupApprovalError, match="CHANGED_DURING_CAPTURE"):
        capture_downloader_mount(client, container.id, "/downloads", root, RUNTIME)
