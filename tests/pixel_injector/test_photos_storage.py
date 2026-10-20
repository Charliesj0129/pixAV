"""The cold read-back runs somewhere that cannot reach the staged bytes.

These tests assert the *shape of the container*, not just its output: a receipt
saying ``cold_inputs: provider-only`` is only worth anything if the process that
produced it could not have opened the staging file.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

from pixav.pixel_injector.photos_storage import (
    COLD_WORK_MOUNT,
    PhotosColdReadback,
)
from pixav.shared.exceptions import ReadbackUnavailableError, VerificationError
from pixav.shared.storage_models import RemoteAssetSegment

ASSET = uuid.UUID("55555555-5555-4555-8555-555555555555")
DIGEST = "b" * 64
SHARE_URL = "https://photos.app.goo.gl/managedcanary"
IMAGE = "pixav-storage-tools:1"
RECEIPT = {"method": "photos-original-browser", "size": 128_743_122, "sha256": DIGEST}


@pytest.mark.parametrize("entry", ["readback", "playback"])
async def test_anonymous_cancellation_waits_for_child_retirement(tmp_path, monkeypatch, entry):
    from pixav.pixel_injector.playback_activity import PhotosPlaybackMaterializer

    started, released, retired, stop_seen = (threading.Event() for _ in range(4))
    if entry == "readback":
        subject = PhotosColdReadback(tmp_path, image="synthetic")
        operation = subject.read_back(segment())
    else:
        subject = PhotosPlaybackMaterializer(tmp_path, image="synthetic")
        operation = subject.materialize({"segments": []}, tmp_path, timeout_seconds=10)

    def run(destination, payload, *, timeout_seconds=None, stopped=None):
        started.set()
        try:
            if stopped is not None:
                assert stopped.wait(5)
                stop_seen.set()
            assert released.wait(5)
            raise RuntimeError("synthetic child retired")
        finally:
            retired.set()

    monkeypatch.setattr(subject, "_run", run)
    running = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(started.wait, 2)
        running.cancel()
        await asyncio.sleep(0.03)
        assert not running.done(), "an isolated child was abandoned on cancellation"
        running.cancel()
        await asyncio.sleep(0.03)
        assert not running.done(), "repeated cancellation bypassed child retirement"
    finally:
        released.set()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(running, 5)
        assert await asyncio.to_thread(retired.wait, 2)
    assert running.cancelled() and stop_seen.is_set()


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
        self.id = "synthetic-cold-child"
        self.stream = FakeStream()
        self.started = False
        self.removed = False
        self._output = output
        self._exit_code = exit_code
        self.attrs = {
            "State": {"Status": "created", "ExitCode": 0},
            "Mounts": [
                {"Destination": destination["bind"], "RW": destination["mode"] == "rw", "Type": "bind"}
                for destination in spec["volumes"].values()
            ],
        }

    def reload(self) -> None:
        return None

    def attach_socket(self, params=None):
        self.params = params
        return self.stream

    def start(self) -> None:
        assert self.stream._sock.shutdown_called, "stdin must be closed before the process runs"
        self.started = True
        self.attrs["State"] = {"Status": "exited", "ExitCode": self._exit_code}

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


@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_child_removal_is_not_successful_retirement(tmp_path, monkeypatch, cancelled):
    client = FakeClient()
    started = threading.Event()
    create = client.create

    def create_with_failed_removal(*args, **kwargs):
        container = create(*args, **kwargs)

        def remove(*args, **kwargs):
            raise OSError("synthetic removal unavailable")

        if cancelled:

            def start():
                container.started = True
                container.attrs["State"] = {"Status": "running", "ExitCode": 0}
                started.set()

            container.start = start
        container.remove = remove
        return container

    monkeypatch.setattr(client, "create", create_with_failed_removal)
    subject = readback(tmp_path, client)
    running = asyncio.create_task(subject.run_anonymous({}, tmp_path, 5))
    if cancelled:
        assert await asyncio.to_thread(started.wait, 2)
        running.cancel()
    expected = asyncio.CancelledError if cancelled else ReadbackUnavailableError
    with pytest.raises(expected):
        await asyncio.wait_for(running, 5)
    assert running.cancelled() == cancelled
    journal = tmp_path.with_name(tmp_path.name + ".cold-child-reconciliation.json")
    record = json.loads(journal.read_text())
    assert record["child_container_id"] == "synthetic-cold-child" and record["removal_confirmed"] is False
    assert record["daemon_identity_revalidation_required"] is True
    assert journal.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ReadbackUnavailableError):
        await subject.run_anonymous({}, tmp_path, 5)
    assert len(client.created) == 1 and not client.created[0].removed


def test_child_reconciliation_record_is_private_and_never_overwritten(tmp_path):
    from pixav.pixel_injector.photos_storage import _record_unconfirmed_child

    work = tmp_path / "session"
    work.mkdir()
    assert _record_unconfirmed_child(work, "first-synthetic-child")
    journal = tmp_path / "session.cold-child-reconciliation.json"
    before = journal.read_bytes()
    assert not _record_unconfirmed_child(work, "replacement-child")
    assert journal.read_bytes() == before and journal.stat().st_mode & 0o777 == 0o600
    assert list(work.iterdir()) == [], "the record must not enter the cold-input mount"


def test_failed_reconciliation_record_does_not_mask_unconfirmed_retirement(tmp_path, monkeypatch):
    from pixav.pixel_injector import photos_storage

    def changed_parent(*args, **kwargs):
        raise ValueError("synthetic directory identity change")

    monkeypatch.setattr(photos_storage, "open_media_directory", changed_parent)
    assert not photos_storage._record_unconfirmed_child(tmp_path / "session", "synthetic-child")


@pytest.mark.parametrize("during_create", [False, True])
def test_stopped_cold_run_never_starts_a_new_child(tmp_path, monkeypatch, during_create):
    client = FakeClient()
    subject = readback(tmp_path, client)
    stopped = threading.Event()
    if during_create:
        create = client.create

        def stop_after_create(*args, **kwargs):
            container = create(*args, **kwargs)
            stopped.set()
            return container

        monkeypatch.setattr(client, "create", stop_after_create)
    else:
        stopped.set()

    with pytest.raises(VerificationError):
        subject._run(tmp_path, b"{}", stopped=stopped)

    assert all(not child.started and child.removed for child in client.created)
    assert len(client.created) == int(during_create)


@pytest.mark.parametrize("secret,manifest", [("configured-private-reference", {}), ("", {"authentication": {}})])
async def test_anonymous_entry_cannot_bypass_authenticated_ownership(tmp_path, monkeypatch, secret, manifest):
    subject = PhotosColdReadback(tmp_path, image="synthetic", authentication_secret=secret)

    def forbidden(*args, **kwargs):
        pytest.fail("authenticated payload reached the anonymous child")

    monkeypatch.setattr(subject, "_run", forbidden)
    with pytest.raises(VerificationError, match="ownership"):
        await subject.run_anonymous(manifest, tmp_path)


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
    assert spec["mem_limit"] == "4g" and spec["nano_cpus"] == 2_000_000_000
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
