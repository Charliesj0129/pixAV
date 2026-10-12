"""The cold read-back runs somewhere that cannot reach the staged bytes.

These tests assert the *shape of the container*, not just its output: a receipt
saying ``cold_inputs: provider-only`` is only worth anything if the process that
produced it could not have opened the staging file.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from pixav.pixel_injector.photos_storage import (
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
        **changes,
    )


async def test_the_container_uses_image_code_and_mounts_only_the_work_dir_bdd_052(tmp_path):
    client = FakeClient()

    receipt = await readback(tmp_path, client).read_back(segment())

    spec = client.created[0].spec
    assert spec["image"] == IMAGE
    assert spec["command"] == ["python", "-m", "pixav.pixel_injector.segment_readback"]
    work = next(source for source, mount in spec["volumes"].items() if mount["bind"] == COLD_WORK_MOUNT)
    assert Path(work).parent == tmp_path / "readback" / str(ASSET) / "3"
    assert Path(work).name.startswith("session-")
    assert spec["volumes"] == {
        work: {"bind": COLD_WORK_MOUNT, "mode": "rw"},
    }
    assert receipt["cold_inputs"] == "provider-only"
    assert receipt["sha256"] == DIGEST


async def test_the_staging_root_is_never_offered_to_the_readback(tmp_path):
    """If the browser could open the staged file, the receipt would prove nothing."""
    client = FakeClient()

    await readback(tmp_path, client).read_back(segment())

    mounted = set(client.created[0].spec["volumes"])
    assert not any("storage-staging" in source for source in mounted)
    assert len(mounted) == 1


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
    sessions = list(work.iterdir())
    assert len(sessions) == 1 and sessions[0].is_dir() and list(sessions[0].iterdir()) == []


async def test_bind_sources_are_translated_for_a_containerised_worker(tmp_path):
    """The daemon resolves a bind source on the host, not inside this worker."""
    client = FakeClient()
    subject = PhotosColdReadback(
        tmp_path / "readback",
        client_factory=lambda: client,
        image=IMAGE,
        host_project_root="/home/operator/pixAV",
        project_root=tmp_path,
    )

    await subject.read_back(segment())

    mounts = sorted(client.created[0].spec["volumes"])
    assert mounts[0].startswith(f"/home/operator/pixAV/readback/{ASSET}/3/session-")
    assert len(mounts) == 1


def test_reconciled_provider_identity_survives_cold_manifest_without_recovery():
    from pixav.shared.storage_models import ProviderSegmentIdentity

    origin = uuid.uuid4()
    item = segment().model_copy(update={"provider_identity": ProviderSegmentIdentity(asset_id=origin, segment_index=2)})
    manifest = PhotosColdReadback.manifest(item)
    restored = RemoteAssetSegment.model_validate(manifest["segment"])
    assert restored.provider_filename == f"pixav-{origin}-part-2-{DIGEST[:16]}.mp4"
    assert restored.filename == item.filename
    assert restored.recovery == {} and restored.verification == {}


async def test_repeated_readback_uses_distinct_empty_workspaces(tmp_path):
    client = FakeClient()
    adapter = readback(tmp_path, client)
    await adapter.read_back(segment())
    await adapter.read_back(segment())
    work = [next(k for k, v in c.spec["volumes"].items() if v["bind"] == COLD_WORK_MOUNT) for c in client.created]
    assert len(set(work)) == 2
    assert all(not list(Path(path).iterdir()) for path in work)


@pytest.mark.parametrize("error", ["TimeoutError", "ModuleNotFoundError", "PermissionError", "Error"])
async def test_incomplete_browser_is_not_a_remote_integrity_verdict(tmp_path, error):
    from pixav.shared.exceptions import ReadbackUnavailableError

    client = FakeClient(output=json.dumps({"status": "BLOCKED", "error_type": error}).encode(), exit_code=2)
    with pytest.raises(ReadbackUnavailableError) as caught:
        await readback(tmp_path, client).read_back(segment())
    assert caught.value.error_type == error


async def test_share_existing_uses_only_retained_runtime_and_no_local_staging(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from pixav.pixel_injector.photos_storage import MaestroSegmentUploader
    from pixav.shared.models import Account

    account = Account(id=uuid.uuid4(), email="fixture@example.invalid")
    calls = []

    @asynccontextmanager
    async def lock(pool, account_id, owner):
        calls.append(("locked", account_id, owner))
        yield lambda: None

    monkeypatch.setattr("pixav.pixel_injector.photos_storage.photos_ui_ownership", lock)
    monkeypatch.setattr(
        "pixav.pixel_injector.photos_storage.stage_segment", lambda *a, **k: pytest.fail("must not stage")
    )
    normal = AsyncMock(side_effect=AssertionError("must not acquire/provision normal runtime"))
    retained = AsyncMock(return_value=(type("Guest", (), {"id": "guest"})(), object()))
    share = AsyncMock(return_value=(SHARE_URL, {"backed_up": True, "original_quality": True}))
    monkeypatch.setattr("pixav.pixel_injector.photos_storage.MaestroPartVerifier.share_selected_existing", share)
    upload = MaestroSegmentUploader(
        normal,
        owner="synthetic-owner",
        flows=tmp_path,
        mode="redroid",
        staging_root=tmp_path,
        ui_pool=object(),
        existing_runtime=retained,
    )
    part = segment(
        account_id=account.id,
        recovery={
            "upload_outcome_review": {"evidence": {"guest_owner": "synthetic-owner", "account_id": str(account.id)}}
        },
    )
    result = await upload.share_existing(part, account, AsyncMock())
    assert result[0] == SHARE_URL and calls == [("locked", account.id, "synthetic-owner")]
    retained.assert_awaited_once_with(account.id)
    normal.assert_not_awaited()
    share.assert_awaited_once()
