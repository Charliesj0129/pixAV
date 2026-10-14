"""Windows routing and halt boundaries; these tests grant no live readiness."""

import hashlib
import threading
import uuid
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pixav.pixel_injector.android_target import CONTAINER_ANDROID_TARGET, AndroidTarget
from pixav.pixel_injector.canary import OWNER_LABEL, CanaryBlockedError
from pixav.pixel_injector.maestro_parts import MaestroPartUploader
from pixav.pixel_injector.session import RedroidSession, WindowsAndroidSession
from pixav.pixel_injector.windows_device import WindowsAndroidDevice
from pixav.shared.models import Account, VideoPart


@pytest.fixture
def windows_upload():
    session = WindowsAndroidSession(
        runtime_id=uuid.uuid4(),
        generation=3,
        owner=uuid.uuid4(),
        account_id=uuid.uuid4(),
        device_key="synthetic-windows-avd",
        identity_sha256="a" * 64,
        avd_name="synthetic-avd",
        boot_id=uuid.uuid4(),
        tools_id="original-tools",
        tools_image="sha256:" + "b" * 64,
        target=AndroidTarget("emulator-5554", "host.docker.internal"),
    )
    runner = SimpleNamespace(
        id=session.tools_id,
        status="running",
        labels={
            OWNER_LABEL: str(session.owner),
            "pixav.photos_canary.role": "tools",
            "pixav.runtime.id": str(session.runtime_id),
            "pixav.runtime.generation": str(session.generation),
        },
        attrs={
            "Config": {
                "Image": session.tools_image,
                "Env": ["ADB_SERVER_SOCKET=tcp:host.docker.internal:5037", "ANDROID_SERIAL=emulator-5554"],
            }
        },
        client=SimpleNamespace(api=SimpleNamespace(timeout=60)),
        reload=Mock(),
        exec_run=Mock(return_value=SimpleNamespace(exit_code=0, output=b"device")),
    )
    runner.stop = Mock(side_effect=lambda **kwargs: setattr(runner, "status", "exited"))
    device = WindowsAndroidDevice(session, runner, verify_native=Mock(), stop_native=AsyncMock())
    video = uuid.uuid4()
    digest = hashlib.sha256(b"synthetic").hexdigest()
    part = VideoPart(
        video_id=video,
        part_index=0,
        manifest_version=1,
        start_seconds=0,
        end_seconds=1,
        size_bytes=9,
        sha256=digest,
        filename=f"pixav-{video}-part-000000-{digest[:16]}.mp4",
        media_info={},
    )
    uploader = MaestroPartUploader(
        None,
        runner,
        str(session.owner),
        part,
        {},
        AsyncMock(),
        Path("config/maestro/photos-canary"),
        windows_device=device,
    )
    uploader.check = Mock()
    return uploader, device


def test_external_session_is_immutable_and_does_not_masquerade_as_a_container(windows_upload):
    _, device = windows_upload
    assert not hasattr(device.session, "container_id")
    assert str(device.session.account_id) not in repr(device.session)
    with pytest.raises(FrozenInstanceError):
        device.session.generation = 4


@pytest.mark.parametrize(
    "changes",
    [
        {"generation": 0},
        {"generation": True},
        {"target": CONTAINER_ANDROID_TARGET},
        {"identity_sha256": "not-a-digest"},
        {"tools_image": "synthetic:latest"},
    ],
)
def test_external_binding_rejects_ambiguous_identity(windows_upload, changes):
    _, device = windows_upload
    with pytest.raises(ValueError):
        replace(device.session, **changes)


@pytest.mark.parametrize("changed", ["runner-id", "owner", "runtime-generation", "image"])
async def test_changed_runner_is_rejected_before_adb_or_native_observation(windows_upload, changed):
    uploader, device = windows_upload
    if changed == "runner-id":
        device.runner.id = "replacement-tools"
    elif changed == "owner":
        device.runner.labels[OWNER_LABEL] = str(uuid.uuid4())
    elif changed == "runtime-generation":
        device.runner.labels["pixav.runtime.generation"] = "4"
    else:
        device.runner.attrs["Config"]["Image"] = "sha256:" + "c" * 64
    with pytest.raises(CanaryBlockedError, match="identity"):
        await uploader.adb("get-state")
    device.runner.exec_run.assert_not_called()
    device._verify_native.assert_not_called()


async def test_every_transport_uses_the_same_explicit_windows_target(windows_upload):
    uploader, device = windows_upload
    assert await uploader.adb("get-state") == "device"
    assert device.runner.exec_run.call_args.args[0] == [
        "timeout",
        "--kill-after=5s",
        "120",
        "adb",
        "-s",
        "emulator-5554",
        "get-state",
    ]
    with patch("pixav.pixel_injector.maestro_parts.hierarchy", return_value=[]) as hierarchy:
        assert await uploader.attributes() == []
        assert hierarchy.call_args.kwargs["target"] == device.session.target
    with patch("pixav.pixel_injector.maestro_parts.run_flow") as flow:
        await uploader.flow("open-photos")
        await uploader.commands(["pressHome"])
        assert all(call.kwargs["target"] == device.session.target for call in flow.call_args_list)


async def test_windows_login_reuses_server_connection_and_checks_account_binding(windows_upload):
    uploader, device = windows_upload
    account = Account(id=device.session.account_id, email="synthetic@example.invalid")
    uploader.adb = AsyncMock(return_value=f"Account {{name={account.email}, type=com.google}}")
    uploader._photos_ready = AsyncMock()
    uploader._sign_in = AsyncMock()
    await uploader.login(device.session, account)
    assert all(call.args[0] != "connect" for call in uploader.adb.await_args_list)
    uploader._sign_in.assert_not_awaited()
    uploader.adb.reset_mock()
    with pytest.raises(CanaryBlockedError, match="account binding"):
        await uploader.login(device.session, Account(email="other@example.invalid"))
    uploader.adb.assert_not_awaited()


def test_wrong_generation_or_legacy_session_cannot_select_external_device(windows_upload):
    uploader, device = windows_upload
    for session in (
        replace(device.session, generation=4),
        RedroidSession(str(device.session.owner), "guest", "127.0.0.1", 5555),
    ):
        with pytest.raises(CanaryBlockedError, match="session changed"):
            uploader.guard(session)
    device._verify_native.assert_not_called()


async def test_windows_effect_requires_ui_ownership(windows_upload):
    uploader, device = windows_upload
    uploader.check = None
    with pytest.raises(CanaryBlockedError, match="UI ownership"):
        await uploader.adb("shell", "input", "keyevent", "3")
    device.runner.exec_run.assert_not_called()


async def test_lost_ui_heartbeat_stops_exact_tools_then_original_native_writers(windows_upload):
    uploader, device = windows_upload
    stopped = threading.Event()
    started = threading.Event()
    events = []
    checks = 0

    def check():
        nonlocal checks
        checks += 1
        if checks > 3:
            raise CanaryBlockedError("synthetic heartbeat loss")

    def stop(**kwargs):
        events.append("tools")
        device.runner.status = "exited"

    async def stop_native(session):
        assert session == device.session
        events.append("native")
        stopped.set()

    def operation():
        started.set()
        assert stopped.wait(5)
        return "partial transfer"

    uploader.check = check
    device.runner.stop = stop
    device._stop_native = stop_native
    with pytest.raises(CanaryBlockedError, match="heartbeat"):
        await uploader._io(operation)
    assert events == ["tools", "native"]
    assert started.is_set()
    assert set(uploader.recovery) == {"runtime_binding"}
    assert uploader.recovery["runtime_binding"]["runtime_generation"] == device.session.generation
    uploader.save.assert_awaited_once()


async def test_native_stop_is_attempted_even_when_tools_stop_fails(windows_upload):
    _, device = windows_upload
    device.runner.stop.side_effect = RuntimeError("synthetic tools stop failure")
    with pytest.raises(RuntimeError, match="stop failure"):
        await device.halt()
    device._stop_native.assert_awaited_once_with(device.session)


async def test_unconfirmed_tools_halt_does_not_claim_writers_stopped(windows_upload):
    _, device = windows_upload
    device.runner.stop.side_effect = None
    with pytest.raises(CanaryBlockedError, match="halt unconfirmed"):
        await device.halt()
    device._stop_native.assert_awaited_once()


async def test_native_identity_loss_stops_writers_without_starting_effect(windows_upload):
    uploader, device = windows_upload
    device._verify_native.side_effect = CanaryBlockedError("synthetic native boot identity changed")
    with pytest.raises(CanaryBlockedError, match="boot identity"):
        await uploader.adb("shell", "input", "keyevent", "3")
    device.runner.exec_run.assert_not_called()
    device.runner.stop.assert_called_once()
    device._stop_native.assert_awaited_once_with(device.session)


async def test_windows_clipboard_uses_verified_adb_and_photos_uid(windows_upload):
    uploader, _ = windows_upload
    uploader.adb = AsyncMock(side_effect=["package:com.google.android.apps.photos uid:10123", "Result: Parcel(NULL)"])
    assert await uploader.clipboard() is None
    assert uploader.adb.await_args_list[0].args[:2] == ("shell", "cmd")
    assert uploader.adb.await_args_list[1].args[1].startswith("/debug_ramdisk/su 10123 -c ")
    assert uploader.guest is None


async def test_runtime_binding_is_journalled_before_first_adb_effect(windows_upload):
    uploader, device = windows_upload

    def execute(command):
        uploader.save.assert_awaited_once()
        assert uploader.recovery["runtime_binding"]["boot_id"] == str(device.session.boot_id)
        return SimpleNamespace(exit_code=0, output=b"device")

    device.runner.exec_run.side_effect = execute
    await uploader.adb("get-state")


@pytest.mark.parametrize("original", [{"push_intent": True}, {"runtime_binding": {"kind": "redroid"}}])
async def test_unreconciled_old_runtime_never_moves_to_windows(windows_upload, original):
    uploader, device = windows_upload
    uploader.recovery.update(original)
    with pytest.raises(CanaryBlockedError, match="reconciliation"):
        await uploader.adb("get-state")
    assert uploader.recovery == original
    device.runner.exec_run.assert_not_called()
    uploader.save.assert_not_awaited()


async def test_rejected_binding_journal_starts_no_ui_and_leaves_no_in_memory_binding(windows_upload):
    uploader, device = windows_upload
    uploader.save.side_effect = CanaryBlockedError("synthetic original activity lease rejected")
    with pytest.raises(CanaryBlockedError, match="lease rejected"):
        await uploader.adb("get-state")
    assert uploader.recovery == {}
    device.runner.exec_run.assert_not_called()
    device._stop_native.assert_awaited_once()


def test_windows_binding_rejects_wrong_uploader_owner_before_claim_or_effect(windows_upload):
    uploader, device = windows_upload
    with pytest.raises(ValueError, match="bound tools"):
        MaestroPartUploader(
            None,
            device.runner,
            str(uuid.uuid4()),
            uploader.part,
            {},
            AsyncMock(),
            uploader.flows,
            windows_device=device,
        )
    device._verify_native.assert_not_called()
    device.runner.exec_run.assert_not_called()
