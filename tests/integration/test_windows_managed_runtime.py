"""Real PostgreSQL admission and authority handoff; native/Docker are adapters."""

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from docker.errors import NotFound

from pixav.maxwell_core.windows_ui_reconciliation import WindowsUIReconciler
from pixav.pixel_injector.canary import OWNER_LABEL, CanaryBlockedError
from pixav.pixel_injector.windows_environment import WindowsEnvironment
from pixav.pixel_injector.windows_host import WindowsHost
from pixav.pixel_injector.windows_host_models import NativeIdentity, WindowsHostManifest, canonical_digest
from pixav.pixel_injector.windows_managed_runtime import WindowsManagedRuntime
from pixav.shared.activity_context import activity_context
from tests.integration.test_external_android_runtime import envelope, request
from tests.pixel_injector.test_windows_environment import environment_data

pytestmark = pytest.mark.integration


@pytest.fixture
async def managed(integration_db, tmp_path):
    pool = integration_db
    for migration in sorted(Path("migrations").glob("*.sql")):
        await pool.execute(migration.read_text())
    runtime_id, owner, account = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    process = {"pid": 100, "created_filetime": "130000000000000000", "sha256": "a" * 64}
    native = NativeIdentity.model_validate(
        {
            "schema": "windows-native-control-v1",
            "sdk_root": r"D:\Synthetic\Sdk",
            "serial": "emulator-5554",
            "avd_name": "synthetic",
            "boot_id": str(uuid.uuid4()),
            "processes": {"adb": process, "emulator": {**process, "pid": 104}, "qemu": {**process, "pid": 108}},
        }
    )
    native = native.model_copy(
        update={"environment": WindowsEnvironment.model_validate(environment_data(native.boot_id, native.avd_name))}
    )
    digest = canonical_digest(native.payload())
    image = "sha256:" + "b" * 64
    staging = tmp_path / "staging"
    await pool.execute("INSERT INTO accounts(id,email) VALUES($1,'synthetic@example.invalid')", account)
    await pool.execute(
        """INSERT INTO upload_runtimes(runtime_id,generation,owner,kind,account_id,device_key,profile,
      identity,identity_sha256,tools_id,tools_image,staging_root,state)
      VALUES($1,1,$2,'windows_android',$3,'synthetic-avd','gphotos_pixel_xl_infinity_v1',$4::jsonb,$5,'synthetic-tools',$6,$7,'READY')""",
        runtime_id,
        owner,
        account,
        json.dumps(native.payload()),
        digest,
        image,
        str(staging),
    )
    runner = SimpleNamespace(
        id="synthetic-tools",
        status="exited",
        labels={
            OWNER_LABEL: str(owner),
            "pixav.photos_canary.role": "tools",
            "pixav.runtime.id": str(runtime_id),
            "pixav.runtime.generation": "1",
        },
        attrs={
            "Config": {
                "Image": image,
                "Env": ["ADB_SERVER_SOCKET=tcp:host.docker.internal:5037", "ANDROID_SERIAL=emulator-5554"],
            }
        },
        reload=Mock(),
    )
    runner.start = Mock(side_effect=lambda: setattr(runner, "status", "running"))
    runner.stop = Mock(side_effect=lambda **kwargs: setattr(runner, "status", "exited"))
    containers = SimpleNamespace(get=Mock(return_value=runner), list=Mock(return_value=[]))
    docker = SimpleNamespace(info=Mock(return_value={"ID": "synthetic-daemon"}), containers=containers)
    calls = []

    def run(mode, subject):
        calls.append(mode)
        result = {
            "observe": "NATIVE_IDENTITY_OBSERVED",
            "quiesce": "NATIVE_UI_QUIESCENT",
            "quiescence": "NATIVE_UI_QUIESCENT",
            "halt": "ORIGINAL_NATIVE_WRITERS_STOPPED",
            "stopped": "ORIGINAL_NATIVE_WRITERS_STOPPED",
        }[mode]
        return {"result": result}

    manifest = WindowsHostManifest(
        runtime_id=runtime_id,
        runtime_generation=1,
        device_key="synthetic-avd",
        identity_sha256=digest,
        tools_id=runner.id,
        native=native,
    )
    host = WindowsHost(manifest, SimpleNamespace(run=run), tmp_path / "host-state.json")
    loop = asyncio.get_running_loop()

    class Bridge:
        def call(self, operation, subject):
            return asyncio.run_coroutine_threadsafe(getattr(host, operation.replace("-", "_"))(subject), loop).result(
                timeout=10
            )

    bridge = Bridge()
    runtime = WindowsManagedRuntime(
        pool,
        docker,
        bridge,
        runtime_id=runtime_id,
        generation=1,
        owner=owner,
        staging_root=staging,
        profile="gphotos_pixel_xl_infinity_v1",
        docker_daemon_id="synthetic-daemon",
    )
    return SimpleNamespace(
        pool=pool,
        runtime=runtime,
        runner=runner,
        docker=docker,
        bridge=bridge,
        host=host,
        binding=(runtime_id, owner, account),
        calls=calls,
    )


async def test_factory_waits_for_authority_halt_verification_before_successor(managed):
    state = managed
    report = await request(state.pool, state.binding)
    original = await envelope(state.pool, report)
    with activity_context(original, uuid.UUID(report["worker_token"])):
        async with state.runtime.acquire(state.binding[2]) as (device, lease):
            lease.check()
            assert device.session.runtime_id == state.binding[0]
            assert device.ui_session_id == str(lease.ticket.id)
            assert state.runner.status == "running"
    assert state.runner.status == "exited"
    assert await state.pool.fetchval("SELECT state FROM photos_ui_sessions") == "HALT_REQUIRED"
    assert state.calls[-1] == "quiesce"
    reconciler = WindowsUIReconciler(state.pool, state.docker, state.bridge, docker_daemon_id="synthetic-daemon")
    assert await reconciler.tick() == 1
    assert await state.pool.fetchval("SELECT state FROM photos_ui_sessions") == "STOPPED"
    assert state.calls[-1] == "quiescence"
    assert await state.pool.fetchval("SELECT operation_id FROM photos_ui_sessions") == original.operation_id
    assert await state.pool.fetchval("SELECT generation FROM photos_ui_sessions") == original.generation
    second = await request(state.pool, state.binding)
    with activity_context(await envelope(state.pool, second), uuid.UUID(second["worker_token"])):
        async with state.runtime.acquire(state.binding[2]):
            pass
    assert await state.pool.fetchval("SELECT count(*) FROM photos_ui_sessions") == 2


async def test_changed_daemon_cannot_attach_or_start_tools(managed):
    state = managed
    state.docker.info.return_value = {"ID": "different-daemon"}
    with pytest.raises(CanaryBlockedError, match="daemon identity"):
        async with state.runtime.acquire(state.binding[2]):
            pytest.fail("attached to a different daemon")
    state.docker.containers.get.assert_not_called()
    state.runner.start.assert_not_called()
    assert await state.pool.fetchval("SELECT count(*) FROM photos_ui_sessions") == 0


async def test_process_only_registration_cannot_start_managed_tools(managed):
    state = managed
    native = state.host.manifest.native.model_copy(update={"environment": None})
    await state.pool.execute(
        """INSERT INTO upload_runtimes(runtime_id,generation,owner,kind,account_id,device_key,profile,
        identity,identity_sha256,tools_id,tools_image,staging_root,state)
        SELECT runtime_id,2,owner,kind,account_id,device_key,profile,
               $1::jsonb,$2,tools_id,tools_image,staging_root,state
          FROM upload_runtimes WHERE runtime_id=$3 AND generation=1""",
        json.dumps(native.payload()),
        canonical_digest(native.payload()),
        state.binding[0],
    )
    state.runtime.generation = 2
    with pytest.raises(CanaryBlockedError, match="identity invalid"):
        async with state.runtime.acquire(state.binding[2]):
            pytest.fail("admitted process-only runtime")
    state.docker.containers.get.assert_not_called()
    state.runner.start.assert_not_called()
    assert await state.pool.fetchval("SELECT count(*) FROM photos_ui_sessions") == 0


async def test_missing_tools_are_not_reprovisioned(managed):
    state = managed
    state.docker.containers.get.side_effect = NotFound("synthetic missing original tools")
    with pytest.raises(CanaryBlockedError, match="missing"):
        async with state.runtime.acquire(state.binding[2]):
            pytest.fail("replaced missing tools")
    assert not state.host.journal.exists()
    assert await state.pool.fetchval("SELECT count(*) FROM photos_ui_sessions") == 0


async def test_halt_receipt_cannot_change_original_generation(managed):
    state = managed
    report = await request(state.pool, state.binding)
    with activity_context(await envelope(state.pool, report), uuid.UUID(report["worker_token"])):
        async with state.runtime.acquire(state.binding[2]):
            pass
    original_call = state.bridge.call

    def altered(operation, subject):
        result = original_call(operation, subject)
        return {**result, "generation": 99} if operation == "verify-halt" else result

    state.bridge.call = altered
    assert (
        await WindowsUIReconciler(state.pool, state.docker, state.bridge, docker_daemon_id="synthetic-daemon").tick()
        == 0
    )
    assert await state.pool.fetchval("SELECT state FROM photos_ui_sessions") == "HALT_REQUIRED"


async def test_cold_reader_unknown_create_keeps_fence_without_starting_android_tools(managed):
    state = managed
    report = await request(state.pool, state.binding, role="cold_read")
    with activity_context(await envelope(state.pool, report), uuid.UUID(report["worker_token"])):
        async with state.runtime.cold(state.binding[2]) as lease:
            lease.child_phase = "CREATE_UNKNOWN"
    state.runner.start.assert_not_called()
    reconciler = WindowsUIReconciler(
        state.pool, state.docker, state.bridge, docker_daemon_id="synthetic-daemon", cold_image="synthetic-cold"
    )
    assert await reconciler.tick() == 0
    assert await state.pool.fetchval("SELECT state FROM photos_ui_sessions") == "HALT_REQUIRED"


async def test_cold_reader_without_effect_can_release_after_independent_observation(managed):
    state = managed
    report = await request(state.pool, state.binding, role="cold_read")
    with activity_context(await envelope(state.pool, report), uuid.UUID(report["worker_token"])):
        async with state.runtime.cold(state.binding[2]):
            pass
    reconciler = WindowsUIReconciler(
        state.pool, state.docker, state.bridge, docker_daemon_id="synthetic-daemon", cold_image="synthetic-cold"
    )
    assert await reconciler.tick() == 1
    assert await state.pool.fetchval("SELECT state FROM photos_ui_sessions") == "STOPPED"


async def test_created_cold_reader_is_retired_before_fence_release(managed):
    state = managed
    report = await request(state.pool, state.binding, role="cold_read")
    with activity_context(await envelope(state.pool, report), uuid.UUID(report["worker_token"])):
        async with state.runtime.cold(state.binding[2]) as lease:
            lease.child_phase = "CREATED"
            lease.child_container_id = "synthetic-reader"
    reader = SimpleNamespace(
        id="synthetic-reader",
        status="created",
        reload=Mock(),
        labels={OWNER_LABEL: str(state.binding[1]), "pixav.photos_canary.role": "cold-readback"},
        attrs={"Config": {"Image": "synthetic-cold"}},
    )
    removed = False

    def remove():
        nonlocal removed
        removed = True

    def get(identity):
        if identity == reader.id:
            if removed:
                raise NotFound("synthetic reader retired")
            return reader
        return state.runner

    reader.remove = Mock(side_effect=remove)
    state.docker.containers.list.return_value = [reader]
    state.docker.containers.get.side_effect = get
    reconciler = WindowsUIReconciler(
        state.pool, state.docker, state.bridge, docker_daemon_id="synthetic-daemon", cold_image="synthetic-cold"
    )
    assert await reconciler.tick() == 1
    reader.remove.assert_called_once_with()
    assert await state.pool.fetchval("SELECT state FROM photos_ui_sessions") == "STOPPED"
