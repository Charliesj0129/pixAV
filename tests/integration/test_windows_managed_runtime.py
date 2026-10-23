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
from pixav.pixel_injector.windows_host_models import (
    HostReadinessReceipt,
    NativeIdentity,
    WindowsHostManifest,
    canonical_digest,
)
from pixav.pixel_injector.windows_managed_runtime import WindowsManagedRuntime
from pixav.pixel_injector.windows_registration import WindowsRegistration, WindowsRuntimeRegistry
from pixav.shared.activity_context import activity_context
from tests.integration.test_external_android_runtime import envelope, halt_evidence, request
from tests.pixel_injector.test_windows_environment import environment_data

pytestmark = pytest.mark.integration


@pytest.fixture
async def managed(integration_db, tmp_path, request):
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
    staging.mkdir()
    stat = staging.stat()
    await pool.execute("INSERT INTO accounts(id,email) VALUES($1,'synthetic@example.invalid')", account)
    runner = SimpleNamespace(
        id="synthetic-tools",
        status="running",
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
            },
            "HostConfig": {"ReadonlyRootfs": True, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"]},
            "Mounts": [{"Type": "bind", "Source": str(staging), "Destination": "/parts", "RW": False}],
        },
        reload=Mock(),
        exec_run=Mock(
            return_value=SimpleNamespace(
                exit_code=0, output=json.dumps({"device": stat.st_dev, "inode": stat.st_ino}).encode()
            )
        ),
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
    host = WindowsHost(manifest, SimpleNamespace(run=run, script_sha256="a" * 64), tmp_path / "host-state.json")
    loop = asyncio.get_running_loop()

    class Bridge:
        def call(self, operation, subject):
            return asyncio.run_coroutine_threadsafe(getattr(host, operation.replace("-", "_"))(subject), loop).result(
                timeout=10
            )

        def readiness(self, subject):
            return HostReadinessReceipt.model_validate(self.call("readiness", subject))

    bridge = Bridge()
    spec = WindowsRegistration(
        host=manifest,
        owner=owner,
        account_id=account,
        tools_image=image,
        docker_daemon_id="synthetic-daemon",
        staging_root=str(staging),
        staging_host_root=str(staging),
        staging_device=stat.st_dev,
        staging_inode=stat.st_ino,
        native_script_sha256="a" * 64,
    )
    registry = WindowsRuntimeRegistry(
        pool,
        docker,
        bridge,
        db_identity=str(await pool.fetchval("SELECT system_identifier FROM pg_control_system()")),
        database=await pool.fetchval("SELECT current_database()"),
    )
    if getattr(request, "param", True):
        await registry.enroll(spec, apply=True, operator="synthetic", reason="contract fixture")
        runner.status = "exited"
        calls.clear()
        docker.containers.get.reset_mock()
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
        spec=spec,
        registry=registry,
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


async def test_observe_scope_keeps_its_role_through_host_and_authority_handoff(managed):
    state = managed
    report = await request(state.pool, state.binding, role="observe")
    with activity_context(await envelope(state.pool, report), uuid.UUID(report["worker_token"])):
        async with state.runtime.acquire(state.binding[2], role="observe"):
            assert state.host.state["binding"]["role"] == "observe"
    assert (
        await WindowsUIReconciler(state.pool, state.docker, state.bridge, docker_daemon_id="synthetic-daemon").tick()
        == 1
    )


async def test_changed_daemon_cannot_attach_or_start_tools(managed):
    state = managed
    state.docker.info.return_value = {"ID": "different-daemon"}
    with pytest.raises(CanaryBlockedError, match="daemon identity"):
        async with state.runtime.acquire(state.binding[2]):
            pytest.fail("attached to a different daemon")
    state.docker.containers.get.assert_not_called()
    state.runner.start.assert_not_called()
    assert await state.pool.fetchval("SELECT count(*) FROM photos_ui_sessions") == 0


async def test_changed_configured_daemon_cannot_reuse_original_enrollment(managed):
    state = managed
    state.runtime.docker_daemon_id = "new-daemon"
    state.docker.info.return_value = {"ID": "new-daemon"}
    with pytest.raises(CanaryBlockedError, match="binding unavailable"):
        async with state.runtime.acquire(state.binding[2]):
            pytest.fail("reused a different daemon")
    assert await state.pool.fetchval("SELECT count(*) FROM photos_ui_sessions") == 0
    state.runner.start.assert_not_called()


async def test_changed_staging_physical_directory_cannot_start_new_writer(managed):
    state = managed
    root = state.runtime.staging_root
    root.rename(root.with_name("previous-staging"))
    root.mkdir()
    with pytest.raises(CanaryBlockedError, match="physical binding changed"):
        async with state.runtime.acquire(state.binding[2]):
            pytest.fail("used an empty replacement staging directory")
    assert await state.pool.fetchval("SELECT count(*) FROM photos_ui_sessions") == 0
    state.runner.start.assert_not_called()


async def test_process_only_registration_cannot_start_managed_tools(managed):
    state = managed
    native = state.host.manifest.native.model_copy(update={"environment": None})
    await state.pool.execute(
        """INSERT INTO upload_runtimes(runtime_id,generation,owner,kind,account_id,device_key,profile,
        identity,identity_sha256,tools_id,tools_image,staging_root,state)
        SELECT runtime_id,2,owner,kind,account_id,device_key,profile,
               $1::jsonb,$2,tools_id,tools_image,staging_root,'REGISTERED'
          FROM upload_runtimes WHERE runtime_id=$3 AND generation=1""",
        json.dumps(native.payload()),
        canonical_digest(native.payload()),
        state.binding[0],
    )
    # Deliberately corrupt a separately enrolled generation as an admin. The
    # factory must still validate the native identity at its adapter boundary.
    await state.pool.execute(
        """INSERT INTO windows_runtime_enrollments
          SELECT runtime_id,2,$2::text,docker_daemon_id,staging_device,staging_inode,
                 native_receipt || jsonb_build_object('runtime_generation',2,'identity_sha256',$2::text),operator,reason,verified_at
        FROM windows_runtime_enrollments WHERE runtime_id=$1 AND generation=1""",
        state.binding[0],
        canonical_digest(native.payload()),
    )
    await state.pool.execute(
        "UPDATE upload_runtimes SET state='READY' WHERE runtime_id=$1 AND generation=2", state.binding[0]
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


async def reviewed_original_upload(state):
    """An authority-accepted original review, still owned by its upload attempt."""
    pool = state.pool
    report = await request(pool, state.binding)
    execution, operation = uuid.UUID(report["execution_id"]), uuid.UUID(report["operation_id"])
    asset, artifact = uuid.uuid4(), uuid.uuid4()
    video = await pool.fetchval(
        "SELECT video_id FROM tasks WHERE id=(SELECT task_id FROM executions WHERE id=$1)", execution
    )
    await pool.execute(
        "INSERT INTO workflow_artifacts(id,execution_id,operation_id,path,facts) VALUES($1,$2,$3,'/synthetic/original.mp4','{}')",
        artifact,
        execution,
        operation,
    )
    await pool.execute(
        """INSERT INTO remote_assets(id,video_id,artifact_id,policy_version,expected,segment_count)
        VALUES($1,$2,$3,'photos-original-v2','{}',1)""",
        asset,
        video,
        artifact,
    )
    review = {
        "status": "ACCEPTED",
        "evidence": {
            "version": "upload-outcome-review-v1",
            "outcome": "EXISTING_ORIGINAL",
            "execution_id": str(execution),
            "operation_id": str(operation),
            "asset_id": str(asset),
            "segment_index": 0,
            "account_id": str(state.binding[2]),
            "guest_owner": str(state.binding[1]),
            "segment_sha256": "c" * 64,
            "generation": report["generation"] - 1,
        },
    }
    await pool.execute(
        """INSERT INTO remote_asset_segments(asset_id,segment_index,start_seconds,end_seconds,size_bytes,
        sha256,local_path,media_info,account_id,state,recovery)
        VALUES($1,0,0,1,1,$2,'/synthetic/original.mp4','{}',$3,'upload_intent',$4::jsonb)""",
        asset,
        "c" * 64,
        state.binding[2],
        json.dumps({"upload_outcome_review": review}),
    )
    await pool.execute("UPDATE operation_intents SET identity=$2 WHERE id=$1", operation, f"upload:{asset}:0")
    await pool.execute(
        "UPDATE executions SET checkpoint=$2::jsonb WHERE id=$1",
        execution,
        json.dumps({"asset_id": str(asset), "upload_outcome_review": review}),
    )
    return report


async def test_existing_original_review_can_use_cold_scope_without_starting_upload_tools(managed):
    state = managed
    report = await reviewed_original_upload(state)
    original = await envelope(state.pool, report)
    with activity_context(original, uuid.UUID(report["worker_token"])):
        async with state.runtime.cold(state.binding[2]) as lease:
            lease.check()
            assert state.host.state["binding"]["role"] == "cold_read"
    state.runner.start.assert_not_called()
    assert await state.pool.fetchval("SELECT role FROM photos_ui_sessions") == "cold_read"
    assert await state.pool.fetchval("SELECT operation_id FROM photos_ui_sessions") == original.operation_id
    assert (
        await WindowsUIReconciler(
            state.pool, state.docker, state.bridge, docker_daemon_id="synthetic-daemon", cold_image="synthetic-cold"
        ).tick()
        == 1
    )


@pytest.mark.parametrize("defect", [None, "pending", "wrong-operation", "wrong-owner", "wrong-hash", "segment-only"])
async def test_only_exact_authority_accepted_original_can_claim_upload_stage_cold_reader(managed, defect):
    state = managed
    report = await reviewed_original_upload(state)
    execution = uuid.UUID(report["execution_id"])
    checkpoint = json.loads(await state.pool.fetchval("SELECT checkpoint FROM executions WHERE id=$1", execution))
    review = checkpoint["upload_outcome_review"]
    if defect == "pending":
        review["status"] = "PENDING"
    elif defect in ("wrong-operation", "wrong-owner", "wrong-hash"):
        key, value = {
            "wrong-operation": ("operation_id", str(uuid.uuid4())),
            "wrong-owner": ("guest_owner", str(uuid.uuid4())),
            "wrong-hash": ("segment_sha256", "d" * 64),
        }[defect]
        review["evidence"][key] = value
    await state.pool.execute(
        "UPDATE remote_asset_segments SET recovery=$1::jsonb", json.dumps({"upload_outcome_review": review})
    )
    if defect == "segment-only":
        checkpoint.pop("upload_outcome_review")
    await state.pool.execute(
        "UPDATE executions SET checkpoint=$2::jsonb WHERE id=$1", execution, json.dumps(checkpoint)
    )
    accepted = await state.pool.fetchval(
        "SELECT acquire_photos_ui_session($1::jsonb)", json.dumps({**report, "role": "cold_read"})
    )
    assert accepted is (defect is None)
    if accepted:
        assert await state.pool.fetchval(
            "SELECT heartbeat_photos_ui_session($1,$2)", uuid.UUID(report["id"]), uuid.UUID(report["token"])
        )
        checkpoint["upload_outcome_review"]["status"] = "REJECTED"
        await state.pool.execute(
            "UPDATE executions SET checkpoint=$2::jsonb WHERE id=$1", execution, json.dumps(checkpoint)
        )
        assert not await state.pool.fetchval(
            "SELECT heartbeat_photos_ui_session($1,$2)", uuid.UUID(report["id"]), uuid.UUID(report["token"])
        )
        assert await state.pool.fetchval("SELECT state FROM photos_ui_sessions") == "HALT_REQUIRED"


async def test_unreviewed_upload_cannot_open_cold_scope_or_start_native_tools(managed):
    state = managed
    report = await request(state.pool, state.binding)
    with activity_context(await envelope(state.pool, report), uuid.UUID(report["worker_token"])):
        with pytest.raises(CanaryBlockedError, match="persistent resource claim rejected"):
            async with state.runtime.cold(state.binding[2]):
                pytest.fail("unreviewed upload obtained a cold reader")
    state.runner.start.assert_not_called()
    assert not state.calls
    assert await state.pool.fetchval("SELECT count(*) FROM photos_ui_sessions") == 0


async def test_original_review_cold_fence_survives_restore_and_quiescent_function_rollback(managed):
    from tests.integration.test_phase0_restore import postgres_tool

    state = managed
    report = {**await reviewed_original_upload(state), "role": "cold_read"}
    assert await state.pool.fetchval("SELECT acquire_photos_ui_session($1::jsonb)", json.dumps(report))
    before = await state.pool.fetchval("SELECT to_jsonb(s)::text FROM photos_ui_sessions s")
    database = await state.pool.fetchval("SELECT current_database()")
    assert database.startswith("pixav_test_")
    dump = postgres_tool("pg_dump", "-U", "pixav_test", "-Fc", "-d", database)
    postgres_tool(
        "pg_restore", "-U", "pixav_test", "--clean", "--if-exists", "--exit-on-error", "-d", database, payload=dump
    )
    assert before == await state.pool.fetchval("SELECT to_jsonb(s)::text FROM photos_ui_sessions s")
    assert await state.pool.fetchval(
        "SELECT heartbeat_photos_ui_session($1,$2)", uuid.UUID(report["id"]), uuid.UUID(report["token"])
    )
    evidence = halt_evidence(report, identity_sha256=state.host.manifest.identity_sha256)
    assert await state.pool.fetchval(
        "SELECT record_verified_photos_ui_halt($1,$2,$3::jsonb)",
        uuid.UUID(report["id"]),
        uuid.UUID(report["token"]),
        json.dumps(evidence),
    )
    old = Path("migrations/022_external_android_runtime.sql").read_text()
    start, end = old.index("CREATE FUNCTION acquire_photos_ui_session("), old.index("-- Only the authority may submit")
    await state.pool.execute(old[start:end].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION"))
    next_report = {**report, "id": str(uuid.uuid4()), "token": str(uuid.uuid4())}
    assert not await state.pool.fetchval("SELECT acquire_photos_ui_session($1::jsonb)", json.dumps(next_report))
    next_report["role"] = "upload"
    assert await state.pool.fetchval("SELECT acquire_photos_ui_session($1::jsonb)", json.dumps(next_report))
    await state.pool.execute(Path("migrations/027_existing_original_review_cold_scope.sql").read_text())
    assert await state.pool.fetchval(
        "SELECT heartbeat_photos_ui_session($1,$2)", uuid.UUID(next_report["id"]), uuid.UUID(next_report["token"])
    )


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
