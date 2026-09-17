"""The cold read-back runs somewhere that cannot reach the staged bytes.

These tests assert the *shape of the container*, not just its output: a receipt
saying ``cold_inputs: provider-only`` is only worth anything if the process that
produced it could not have opened the staging file.
"""

from __future__ import annotations

import json
import uuid

import pytest

from pixav.pixel_injector.photos_storage import (
    COLD_SOURCE_MOUNT,
    COLD_WORK_MOUNT,
    PhotosColdReadback,
)
from pixav.shared.exceptions import VerificationError
from pixav.shared.storage_models import RemoteAssetSegment

ASSET = uuid.UUID("55555555-5555-4555-8555-555555555555")
DIGEST = "b" * 64
SHARE_URL = "https://photos.app.goo.gl/managedcanary"
IMAGE = "pixav-storage-tools:1"
RECEIPT = {"method": "photos-original-browser", "size": 128_743_122, "sha256": DIGEST}


def segment(**overrides) -> RemoteAssetSegment:
    fields = {
        "asset_id": ASSET,
        "segment_index": 3,
        "start_seconds": 0.0,
        "end_seconds": 100.0,
        "size_bytes": 128_743_122,
        "sha256": DIGEST,
        "local_path": "/app/data/storage-staging/asset/part.mp4",
        "share_url": SHARE_URL,
        "account_id": uuid.UUID("77777777-7777-4777-8777-777777777777"),
        "recovery": {"operations": {"push": {"completed_at": "2026-09-15T00:00:00Z"}}},
        **overrides,
    }
    return RemoteAssetSegment(**fields)


class FakeSocket:
    def __init__(self) -> None:
        self.sent = b""
        self.shutdown_called = False

    def sendall(self, payload: bytes) -> None:
        self.sent += payload

    def shutdown(self, _how: int) -> None:
        self.shutdown_called = True


class FakeStream:
    def __init__(self) -> None:
        self._sock = FakeSocket()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeContainer:
    def __init__(self, spec: dict, *, output: bytes, exit_code: int = 0) -> None:
        self.spec = spec
        self.stream = FakeStream()
        self.started = False
        self.removed = False
        self._output = output
        self._exit_code = exit_code
        self.attrs = {
            "Mounts": [
                {"Destination": destination["bind"], "RW": destination["mode"] == "rw", "Type": "bind"}
                for destination in spec["volumes"].values()
            ]
        }

    def reload(self) -> None:
        return None

    def attach_socket(self, params=None):
        self.params = params
        return self.stream

    def start(self) -> None:
        assert self.stream._sock.shutdown_called, "stdin must be closed before the process runs"
        self.started = True

    def wait(self, timeout=None):
        return {"StatusCode": self._exit_code}

    def logs(self, stdout=True, stderr=False):
        return self._output if stdout else b""

    def remove(self, force=False) -> None:
        self.removed = True


class FakeClient:
    def __init__(self, *, output: bytes = json.dumps(RECEIPT).encode(), exit_code: int = 0) -> None:
        self.created: list[FakeContainer] = []
        self._output = output
        self._exit_code = exit_code
        self.containers = self

    def create(self, image, **kwargs):
        container = FakeContainer({"image": image, **kwargs}, output=self._output, exit_code=self._exit_code)
        self.created.append(container)
        return container


def readback(tmp_path, client, **changes) -> PhotosColdReadback:
    return PhotosColdReadback(
        tmp_path / "readback",
        client_factory=lambda: client,
        image=IMAGE,
        source_root=tmp_path / "src",
        **changes,
    )


async def test_the_container_mounts_only_the_work_dir_and_the_source_tree_bdd_052(tmp_path):
    client = FakeClient()

    receipt = await readback(tmp_path, client).read_back(segment())

    spec = client.created[0].spec
    assert spec["image"] == IMAGE
    assert spec["command"] == ["python", "-m", "pixav.pixel_injector.segment_readback"]
    assert spec["volumes"] == {
        str(tmp_path / "src"): {"bind": COLD_SOURCE_MOUNT, "mode": "ro"},
        str(tmp_path / "readback" / str(ASSET) / "3"): {"bind": COLD_WORK_MOUNT, "mode": "rw"},
    }
    assert receipt["cold_inputs"] == "provider-only"
    assert receipt["sha256"] == DIGEST


async def test_the_staging_root_is_never_offered_to_the_readback(tmp_path):
    """If the browser could open the staged file, the receipt would prove nothing."""
    client = FakeClient()

    await readback(tmp_path, client).read_back(segment())

    mounted = set(client.created[0].spec["volumes"])
    assert not any("storage-staging" in source for source in mounted)
    assert len(mounted) == 2


async def test_an_unexpected_mount_fails_the_readback(tmp_path, monkeypatch):
    client = FakeClient()

    def smuggled(container):
        container.attrs["Mounts"].append({"Destination": "/parts", "RW": False, "Type": "bind"})

    original_create = client.create

    def create(image, **kwargs):
        container = original_create(image, **kwargs)
        smuggled(container)
        return container

    client.create = create

    with pytest.raises(VerificationError, match="unexpected mounts"):
        await readback(tmp_path, client).read_back(segment())
    assert client.created[0].removed is True


async def test_the_manifest_travels_on_stdin_without_a_local_path_or_account(tmp_path):
    client = FakeClient()

    await readback(tmp_path, client).read_back(segment())

    sent = json.loads(client.created[0].stream._sock.sent.decode())
    assert sent["segment"]["share_url"] == SHARE_URL
    assert sent["segment"]["local_path"] == ""
    assert sent["segment"]["account_id"] is None
    assert sent["segment"]["recovery"] == {}


async def test_the_container_is_always_removed_and_never_reused(tmp_path):
    client = FakeClient()

    await readback(tmp_path, client).read_back(segment())

    assert client.created[0].removed is True
    assert client.created[0].started is True


async def test_a_blocked_child_becomes_a_verification_error_without_detail(tmp_path):
    client = FakeClient(output=json.dumps({"status": "BLOCKED", "error_type": "ValueError"}).encode(), exit_code=2)

    with pytest.raises(VerificationError) as failure:
        await readback(tmp_path, client).read_back(segment())

    assert "ValueError" in str(failure.value)
    assert SHARE_URL not in str(failure.value)


async def test_a_symlinked_readback_root_is_refused_bdd_052(tmp_path):
    root = tmp_path / "readback"
    (tmp_path / "elsewhere").mkdir()
    root.symlink_to(tmp_path / "elsewhere")

    with pytest.raises(VerificationError, match="symlink"):
        await readback(tmp_path, FakeClient()).read_back(segment())


async def test_the_work_dir_is_this_segment_s_own_and_starts_empty(tmp_path):
    client = FakeClient()

    await readback(tmp_path, client).read_back(segment())

    work = tmp_path / "readback" / str(ASSET) / "3"
    assert work.is_dir() and list(work.iterdir()) == []


async def test_bind_sources_are_translated_for_a_containerised_worker(tmp_path):
    """The daemon resolves a bind source on the host, not inside this worker."""
    client = FakeClient()
    subject = PhotosColdReadback(
        tmp_path / "readback",
        client_factory=lambda: client,
        image=IMAGE,
        source_root=tmp_path / "src",
        host_project_root="/home/operator/pixAV",
        project_root=tmp_path,
    )

    await subject.read_back(segment())

    assert sorted(client.created[0].spec["volumes"]) == [
        f"/home/operator/pixAV/readback/{ASSET}/3",
        "/home/operator/pixAV/src",
    ]
