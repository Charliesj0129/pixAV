"""Host ownership survives failures and cannot be reopened by stale requests."""

import asyncio
import hashlib
import json
import threading
import uuid
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from pixav.pixel_injector.windows_host import NativeControl, NativeControlError, WindowsHost, create_app
from pixav.pixel_injector.windows_host_models import (
    HostUIRequest,
    NativeIdentity,
    WindowsHostManifest,
    canonical_digest,
)
from tests.pixel_injector.test_windows_environment import environment_data


@pytest.fixture
def manifest():
    process = {"pid": 100, "created_filetime": "130000000000000000", "sha256": "a" * 64}
    native = NativeIdentity.model_validate(
        {
            "schema": "windows-native-control-v1",
            "sdk_root": r"D:\Synthetic\Sdk",
            "serial": "emulator-5554",
            "avd_name": "synthetic-avd",
            "boot_id": str(uuid.uuid4()),
            "processes": {"adb": process, "emulator": {**process, "pid": 104}, "qemu": {**process, "pid": 108}},
        }
    )
    native = NativeIdentity.model_validate(
        {**native.payload(), "environment": environment_data(native.boot_id, native.avd_name)}
    )
    return WindowsHostManifest(
        runtime_id=uuid.uuid4(),
        runtime_generation=3,
        device_key="synthetic-avd",
        identity_sha256=canonical_digest(native.payload()),
        tools_id="synthetic-tools",
        native=native,
    )


def request(manifest, **changes):
    return HostUIRequest(
        runtime_id=manifest.runtime_id,
        runtime_generation=manifest.runtime_generation,
        identity_sha256=manifest.identity_sha256,
        session_id=uuid.uuid4(),
        session_token=uuid.uuid4(),
        operation_id=uuid.uuid4(),
        generation=7,
        **changes,
    )


@pytest.fixture
def host(manifest, tmp_path):
    def run(mode, subject):
        result = {
            "observe": "NATIVE_IDENTITY_OBSERVED",
            "quiesce": "NATIVE_UI_QUIESCENT",
            "quiescence": "NATIVE_UI_QUIESCENT",
            "halt": "ORIGINAL_NATIVE_WRITERS_STOPPED",
            "stopped": "ORIGINAL_NATIVE_WRITERS_STOPPED",
        }[mode]
        return {"result": result}

    return WindowsHost(manifest, SimpleNamespace(run=run), tmp_path / "private" / "state.json")


async def test_host_claim_is_idempotent_and_rejects_overlapping_ticket(host):
    first = request(host.manifest)
    assert await host.claim(first) == await host.claim(first)
    with pytest.raises(NativeControlError, match="TICKET_REJECTED"):
        await host.claim(request(host.manifest))
    stored = json.loads(host.journal.read_text())
    assert str(first.session_token) not in host.journal.read_text()
    assert stored["binding"]["operation_id"] == str(first.operation_id)
    assert stored["binding"]["generation"] == 7


async def test_process_restart_keeps_active_fence(host):
    original = request(host.manifest)
    await host.claim(original)
    restarted = WindowsHost(host.manifest, host.control, host.journal)
    with pytest.raises(NativeControlError):
        await restarted.claim(request(host.manifest))
    assert (await restarted.observe(original))["state"] == "ACTIVE"


async def test_failed_native_observation_does_not_release_host_ownership(host):
    original = request(host.manifest)

    def failed(*_):
        raise NativeControlError("synthetic native observation failed")

    host.control.run = failed
    with pytest.raises(NativeControlError):
        await host.claim(original)
    assert json.loads(host.journal.read_text())["state"] == "ACTIVE"
    with pytest.raises(NativeControlError):
        await host.claim(request(host.manifest))


async def test_halted_ticket_cannot_reopen_even_after_another_session(host):
    first, second = request(host.manifest), request(host.manifest)
    await host.claim(first)
    await host.halt(first)
    with pytest.raises(NativeControlError, match="RETIRED"):
        await host.claim(first)
    await host.claim(second)
    await host.halt(second)
    restarted = WindowsHost(host.manifest, host.control, host.journal)
    with pytest.raises(NativeControlError, match="RETIRED"):
        await restarted.claim(first)


async def test_unconfirmed_halt_never_reopens_or_verifies_ticket(host):
    original = request(host.manifest, cold=True)
    await host.claim(original)

    def failed(*_):
        raise NativeControlError("synthetic halt unknown")

    host.control.run = failed
    with pytest.raises(NativeControlError):
        await host.halt(original)
    assert json.loads(host.journal.read_text())["state"] == "HALT_REQUIRED"
    with pytest.raises(NativeControlError, match="RECONCILIATION"):
        await host.claim(original)
    with pytest.raises(NativeControlError, match="NOT_RECORDED"):
        await host.verify_halt(original)


async def test_verify_halt_performs_fresh_read_only_observation(host):
    original = request(host.manifest)
    await host.claim(original)
    await host.halt(original)
    calls = []

    def observe(mode, subject):
        calls.append(mode)
        return {"result": "NATIVE_UI_QUIESCENT"}

    host.control.run = observe
    result = await host.verify_halt(original)
    assert calls == ["quiescence"]
    assert result["operation_id"] == str(original.operation_id)
    assert result["generation"] == 7 and result["native_writers_stopped"] is True


async def test_native_cancellation_keeps_host_lock_until_child_finishes(host):
    original = request(host.manifest)
    entered, release = threading.Event(), threading.Event()

    def blocked(mode, subject):
        entered.set()
        assert release.wait(5)
        return {"result": "NATIVE_UI_QUIESCENT"}

    host.control.run = blocked
    task = asyncio.create_task(host.claim(original))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert host.lock.locked() and not task.done()
        release.set()
        assert (await asyncio.wait_for(task, 5))["state"] == "ACTIVE"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_bad_runtime_generation_starts_no_native_operation(host):
    bad = request(host.manifest).model_copy(update={"runtime_generation": 4})
    with pytest.raises(NativeControlError, match="BINDING_CHANGED"):
        await host.claim(bad)
    assert not host.journal.exists()


async def test_http_contract_rejects_unbounded_and_secret_echoing_payloads(host):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(host)), base_url="https://synthetic"
    ) as client:
        response = await client.post("/v1/claim", content=b"x" * 16385)
        assert response.status_code == 413
        response = await client.post("/v1/claim", json={"session_token": "synthetic-private-token"})
        assert response.status_code == 400 and "synthetic-private-token" not in response.text


def test_manifest_cannot_bind_a_different_native_identity(manifest):
    value = deepcopy(manifest.model_dump(mode="json"))
    value["native"]["boot_id"] = str(uuid.uuid4())
    value["native"]["environment"]["boot_id"] = value["native"]["boot_id"]
    with pytest.raises(ValueError, match="digest mismatch"):
        WindowsHostManifest.model_validate(value)


async def test_process_only_registration_cannot_admit_new_host_ticket(host):
    native = host.manifest.native.model_copy(update={"environment": None})
    host.manifest = host.manifest.model_copy(
        update={"native": native, "identity_sha256": canonical_digest(native.payload())}
    )
    with pytest.raises(NativeControlError, match="ENVIRONMENT"):
        await host.claim(request(host.manifest))
    assert not host.journal.exists()


def test_native_observation_requires_actual_environment_verification(manifest):
    control = object.__new__(NativeControl)
    control.manifest = manifest
    response = {**manifest.native.payload(), "result": "NATIVE_IDENTITY_OBSERVED"}
    with pytest.raises(NativeControlError, match="ENVIRONMENT_UNCONFIRMED"):
        control._validate_response("observe", response)
    response["environment_verified"] = True
    control._validate_response("observe", response)


async def test_legacy_process_identity_can_still_reconcile_original_halt(host):
    native = host.manifest.native.model_copy(update={"environment": None})
    legacy = host.manifest.model_copy(update={"native": native, "identity_sha256": canonical_digest(native.payload())})
    original = request(legacy)
    host._persist(
        {
            "state": "ACTIVE",
            "binding": original.public_binding(),
            "token_sha256": hashlib.sha256(original.session_token.bytes).hexdigest(),
        }
    )
    restarted = WindowsHost(legacy, host.control, host.journal)
    receipt = await restarted.halt(original)
    assert receipt["operation_id"] == str(original.operation_id)
    assert receipt["generation"] == original.generation
    assert await restarted.verify_halt(original) == receipt


def test_broken_environment_blocks_observation_but_retains_exact_emergency_halt(manifest, tmp_path, monkeypatch):
    import pixav.pixel_injector.windows_host as module

    def unavailable(*_):
        raise OSError("synthetic missing environment file")

    monkeypatch.setattr(module, "EnvironmentFiles", unavailable)
    script = tmp_path / "control.ps1"
    script.write_text("# synthetic pinned script")
    control = NativeControl(script, manifest, script_sha256=hashlib.sha256(script.read_bytes()).hexdigest())
    original = request(manifest, cold=True)
    with pytest.raises(NativeControlError, match="ENVIRONMENT_REVIEW_REQUIRED"):
        control.run("observe", original)
    response = manifest.native.payload()
    response["processes"].pop("adb")
    response["result"] = "ORIGINAL_NATIVE_WRITERS_STOPPED"
    for process in response["processes"].values():
        process["original_process_absent"] = True
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(response).encode())
    )
    assert control.run("halt", original)["result"] == "ORIGINAL_NATIVE_WRITERS_STOPPED"


def test_legacy_native_digest_does_not_gain_a_null_environment_field(manifest):
    value = manifest.native.payload()
    value.pop("environment")
    legacy = NativeIdentity.model_validate(value)
    assert legacy.payload() == value
    assert canonical_digest(legacy.payload()) == canonical_digest(value)


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (b'{"code":"NATIVE_LAUNCH_BINDING_MISMATCH"}', "NATIVE_LAUNCH_BINDING_MISMATCH"),
        (b'{"code":"synthetic private payload"}', "NATIVE_OBSERVATION_REJECTED"),
        (b"synthetic private payload", "NATIVE_OBSERVATION_REJECTED"),
        (b"[]", "NATIVE_OBSERVATION_REJECTED"),
        (b"x" * 4097, "NATIVE_OBSERVATION_REJECTED"),
    ],
)
def test_only_bounded_native_error_codes_cross_host_boundary(manifest, tmp_path, monkeypatch, stderr, expected):
    import pixav.pixel_injector.windows_host as module

    script = tmp_path / "control.ps1"
    script.write_text("# synthetic pinned script")
    control = NativeControl(script, manifest, script_sha256=hashlib.sha256(script.read_bytes()).hexdigest())
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout=b"", stderr=stderr)
    )
    with pytest.raises(NativeControlError) as error:
        control.run("halt", request(manifest, cold=True))
    assert str(error.value) == expected
