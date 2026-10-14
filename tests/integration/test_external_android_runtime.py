"""Persistent UI fences survive connection loss and preserve original intents."""

import asyncio
import json
import uuid
from pathlib import Path

import asyncpg
import pytest

from pixav.pixel_injector.android_target import AndroidTarget
from pixav.pixel_injector.canary import CanaryBlockedError
from pixav.pixel_injector.session import WindowsAndroidSession
from pixav.shared.activity_context import activity_context
from pixav.shared.photos_ui_ownership import photos_ui_ownership
from pixav.shared.photos_ui_session import photos_ui_session
from pixav.shared.workflow import ActivityRequest
from scripts.backup_files import create_backup_file

pytestmark = pytest.mark.integration

EXPAND = Path("migrations/022_external_android_runtime.sql")


async def base_schema(pool):
    for path in sorted(Path("migrations").glob("*.sql")):
        if path.name < EXPAND.name:
            await pool.execute(path.read_text())


@pytest.fixture
async def runtime_db(integration_db):
    await base_schema(integration_db)
    await integration_db.execute(EXPAND.read_text())
    return integration_db


@pytest.fixture
async def ui_session_db(runtime_db):
    await runtime_db.execute(Path("migrations/023_photos_ui_halt_requests.sql").read_text())
    return runtime_db


def native_session(binding):
    runtime_id, owner, account = binding
    return WindowsAndroidSession(
        runtime_id=runtime_id,
        generation=1,
        owner=owner,
        account_id=account,
        device_key="synthetic-avd",
        identity_sha256="a" * 64,
        avd_name="synthetic-avd",
        boot_id=uuid.uuid4(),
        tools_id="synthetic-tools",
        tools_image="sha256:" + "b" * 64,
        target=AndroidTarget("emulator-5554", "host.docker.internal"),
    )


async def envelope(pool, report):
    return ActivityRequest(
        task_id=await pool.fetchval("SELECT task_id FROM executions WHERE id=$1", uuid.UUID(report["execution_id"])),
        execution_id=report["execution_id"],
        attempt_id=report["attempt_id"],
        operation_id=report["operation_id"],
        owner=report["owner"],
        generation=report["generation"],
        stage="upload" if report["role"] == "upload" else "verify",
        identity="original-operation",
        account_id=report["account_id"],
    )


async def runtime(pool, *, account=None, device="synthetic-avd", owner=None):
    account = account or uuid.uuid4()
    await pool.execute(
        "INSERT INTO accounts(id,email) VALUES($1,$2) ON CONFLICT DO NOTHING", account, f"{account}@example.invalid"
    )
    identity = uuid.uuid4()
    owner = owner or uuid.uuid4()
    await pool.execute(
        """INSERT INTO upload_runtimes(runtime_id,generation,owner,kind,account_id,device_key,profile,
        identity,identity_sha256,tools_id,tools_image,staging_root,state)
        VALUES($1,1,$2,'windows_android',$3,$4,'synthetic-pixel-profile',
        '{"schema":"windows-android-runtime-v1","avd":"synthetic"}',
        $5,'synthetic-tools','sha256:synthetic-tools','/synthetic','READY')""",
        identity,
        owner,
        account,
        device,
        "a" * 64,
    )
    return identity, owner, account


async def request(pool, binding, *, role="upload"):
    runtime_id, runtime_owner, account = binding
    video = await pool.fetchval("INSERT INTO videos(title) VALUES('synthetic UI fence') RETURNING id")
    task = await pool.fetchval("INSERT INTO tasks(video_id) VALUES($1) RETURNING id", video)
    await pool.execute("INSERT INTO workflow_tasks(task_id,video_id) VALUES($1,$2)", task, video)
    execution, attempt, operation, worker, token = [uuid.uuid4() for _ in range(5)]
    stage = "upload" if role == "upload" else "verify"
    await pool.execute(
        """INSERT INTO executions(id,task_id,state,stage,generation,owner,lease_until,max_retries)
        VALUES($1,$2,'RUNNING',$3,7,$4,now()+interval '10 minutes',3)""",
        execution,
        task,
        stage,
        worker,
    )
    await pool.execute(
        "INSERT INTO operation_intents(id,execution_id,stage,identity) VALUES($1,$2,$3,'original-operation')",
        operation,
        execution,
        stage,
    )
    await pool.execute(
        """INSERT INTO activity_attempts(id,execution_id,generation,owner,stage,operation_id,worker_token)
        VALUES($1,$2,7,$3,$4,$5,$6)""",
        attempt,
        execution,
        worker,
        stage,
        operation,
        token,
    )
    return {
        "id": str(uuid.uuid4()),
        "token": str(uuid.uuid4()),
        "runtime_id": str(runtime_id),
        "runtime_generation": 1,
        "runtime_owner": str(runtime_owner),
        "account_id": str(account),
        "role": role,
        "execution_id": str(execution),
        "attempt_id": str(attempt),
        "operation_id": str(operation),
        "generation": 7,
        "owner": str(worker),
        "worker_token": str(token),
    }


async def claim(pool, report):
    return await pool.fetchval("SELECT acquire_photos_ui_session($1::jsonb)", json.dumps(report))


def halt_evidence(report, **changes):
    return {
        "schema": "windows-ui-halt-v1",
        "runtime_id": report["runtime_id"],
        "runtime_generation": 1,
        "operation_id": report["operation_id"],
        "generation": 7,
        "device_key": "synthetic-avd",
        "tools_id": "synthetic-tools",
        "identity_sha256": "a" * 64,
        "runner_stopped": True,
        "native_writers_stopped": True,
        "receipt_sha256": "b" * 64,
        **changes,
    }


async def test_expand_preserves_legacy_runtime_and_requires_reconciliation(integration_db):
    await base_schema(integration_db)
    owner = uuid.uuid4()
    await integration_db.execute(
        """INSERT INTO guest_runtimes(owner,state,profile,guest_image,tools_image,guest_id,tools_id,staging_root)
        VALUES($1,'READY','synthetic-pixel','synthetic-guest','synthetic-tools','original-guest','original-tools','/synthetic')""",
        owner,
    )
    before = await integration_db.fetchval("SELECT to_jsonb(g)::text FROM guest_runtimes g WHERE owner=$1", owner)
    await integration_db.execute(EXPAND.read_text())
    assert before == await integration_db.fetchval(
        "SELECT to_jsonb(g)::text FROM guest_runtimes g WHERE owner=$1", owner
    )
    row = await integration_db.fetchrow("SELECT * FROM upload_runtimes WHERE runtime_id=$1", owner)
    assert row["kind"] == "redroid" and row["state"] == "REVIEW_REQUIRED"
    assert json.loads(row["identity"])["guest_id"] == "original-guest"


async def test_ui_fence_persists_after_connection_close_and_expiry(runtime_db):
    binding = await runtime(runtime_db)
    first = await request(runtime_db, binding)
    async with runtime_db.acquire() as conn:
        assert await claim(conn, first)
        backend = await conn.fetchval("SELECT pg_backend_pid()")
        conn.terminate()
    assert await runtime_db.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid=$1", backend) == 0
    await runtime_db.execute("UPDATE photos_ui_sessions SET lease_until=now()-interval '1 hour'")
    second = await request(runtime_db, binding)
    assert not await claim(runtime_db, second)
    assert await runtime_db.fetchval("SELECT state FROM photos_ui_sessions") == "ACTIVE"
    assert await runtime_db.fetchval("SELECT operation_id FROM photos_ui_sessions") == uuid.UUID(first["operation_id"])
    assert await runtime_db.fetchval("SELECT generation FROM photos_ui_sessions") == 7


@pytest.mark.parametrize("shared", ["account", "device", "upload-slot"])
async def test_account_device_and_global_upload_slot_reject_overlapping_writers(runtime_db, shared):
    first_binding = await runtime(runtime_db)
    first = await request(runtime_db, first_binding)
    assert await claim(runtime_db, first)
    other = await runtime(
        runtime_db,
        account=first_binding[2] if shared == "account" else None,
        device="synthetic-avd" if shared == "device" else "second-avd",
    )
    role = "upload" if shared == "upload-slot" else "cold_read"
    assert not await claim(runtime_db, await request(runtime_db, other, role=role))


@pytest.mark.parametrize(
    "field,value", [("generation", 8), ("runtime_generation", 2), ("worker_token", str(uuid.UUID(int=1)))]
)
async def test_changed_activity_or_runtime_binding_cannot_claim_ui(runtime_db, field, value):
    report = await request(runtime_db, await runtime(runtime_db))
    assert not await claim(runtime_db, {**report, field: value})
    assert await runtime_db.fetchval("SELECT count(*) FROM photos_ui_sessions") == 0


async def test_halt_requires_exact_original_binding_and_authority(runtime_db):
    binding = await runtime(runtime_db)
    first = await request(runtime_db, binding)
    assert await claim(runtime_db, first)
    args = uuid.UUID(first["id"]), uuid.UUID(first["token"])
    bad = halt_evidence(first, generation=8)
    assert not await runtime_db.fetchval(
        "SELECT record_verified_photos_ui_halt($1,$2,$3::jsonb)", *args, json.dumps(bad)
    )
    async with runtime_db.acquire() as conn:
        await conn.execute("SET ROLE pixav_activity_worker")
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.fetchval(
                    "SELECT record_verified_photos_ui_halt($1,$2,$3::jsonb)", *args, json.dumps(halt_evidence(first))
                )
        finally:
            await conn.execute("RESET ROLE")
    assert await runtime_db.fetchval(
        "SELECT record_verified_photos_ui_halt($1,$2,$3::jsonb)", *args, json.dumps(halt_evidence(first))
    )
    assert await claim(runtime_db, await request(runtime_db, binding))


async def test_identical_claim_is_idempotent_but_another_token_is_rejected(runtime_db):
    report = await request(runtime_db, await runtime(runtime_db))
    assert await claim(runtime_db, report)
    assert await claim(runtime_db, report)
    assert not await claim(runtime_db, {**report, "token": str(uuid.uuid4())})
    assert await runtime_db.fetchval("SELECT count(*) FROM photos_ui_sessions") == 1


async def test_runtime_identity_cannot_change_in_place(runtime_db):
    binding = await runtime(runtime_db)
    with pytest.raises(asyncpg.CheckViolationError, match="new generation"):
        await runtime_db.execute(
            "UPDATE upload_runtimes SET device_key='replacement-avd' WHERE runtime_id=$1", binding[0]
        )
    assert await runtime_db.fetchval("SELECT device_key FROM upload_runtimes") == "synthetic-avd"


@pytest.mark.parametrize("invalidated", ["execution", "runtime"])
async def test_heartbeat_loss_requests_halt_and_keeps_replacement_blocked(runtime_db, invalidated):
    binding = await runtime(runtime_db)
    report = await request(runtime_db, binding)
    assert await claim(runtime_db, report)
    if invalidated == "execution":
        await runtime_db.execute("UPDATE executions SET state='WAITING_RETRY'")
    else:
        await runtime_db.execute("UPDATE upload_runtimes SET state='REVIEW_REQUIRED',review_reason='synthetic hold'")
    assert not await runtime_db.fetchval(
        "SELECT heartbeat_photos_ui_session($1,$2)", uuid.UUID(report["id"]), uuid.UUID(report["token"])
    )
    assert await runtime_db.fetchval("SELECT state FROM photos_ui_sessions") == "HALT_REQUIRED"
    assert not await claim(runtime_db, await request(runtime_db, binding))


async def test_same_runtime_owner_cannot_use_two_devices_or_accounts(runtime_db):
    first = await runtime(runtime_db)
    assert await claim(runtime_db, await request(runtime_db, first))
    second = await runtime(runtime_db, owner=first[1], device="second-avd")
    assert not await claim(runtime_db, await request(runtime_db, second, role="cold_read"))


@pytest.mark.parametrize("invalidated", ["session-lease", "stage"])
async def test_stale_session_cannot_renew_its_ui_lease(runtime_db, invalidated):
    binding = await runtime(runtime_db)
    report = await request(runtime_db, binding)
    assert await claim(runtime_db, report)
    if invalidated == "session-lease":
        await runtime_db.execute("UPDATE photos_ui_sessions SET lease_until=now()-interval '1 second'")
    else:
        await runtime_db.execute("UPDATE executions SET stage='verify'")
    assert not await runtime_db.fetchval(
        "SELECT heartbeat_photos_ui_session($1,$2)", uuid.UUID(report["id"]), uuid.UUID(report["token"])
    )
    assert await runtime_db.fetchval("SELECT state FROM photos_ui_sessions") == "HALT_REQUIRED"
    assert not await claim(runtime_db, await request(runtime_db, binding))


async def test_full_dump_restore_preserves_active_fence_and_original_operation(runtime_db, tmp_path):
    from tests.integration.test_phase0_restore import postgres_tool

    binding = await runtime(runtime_db)
    original = await request(runtime_db, binding)
    assert await claim(runtime_db, original)
    before = await runtime_db.fetchval("SELECT to_jsonb(s)::text FROM photos_ui_sessions s")
    identity = await runtime_db.fetchval("SELECT to_jsonb(r)::text FROM upload_runtimes r")
    database = await runtime_db.fetchval("SELECT current_database()")
    assert database.startswith("pixav_test_")
    dump = tmp_path / "external-ui-full.dump"
    with create_backup_file(dump, binary=True) as handle:
        handle.write(postgres_tool("pg_dump", "-U", "pixav_test", "-Fc", "-d", database))
    postgres_tool(
        "pg_restore",
        "-U",
        "pixav_test",
        "--clean",
        "--if-exists",
        "--exit-on-error",
        "-d",
        database,
        payload=dump.read_bytes(),
    )
    assert before == await runtime_db.fetchval("SELECT to_jsonb(s)::text FROM photos_ui_sessions s")
    assert identity == await runtime_db.fetchval("SELECT to_jsonb(r)::text FROM upload_runtimes r")
    assert not await claim(runtime_db, await request(runtime_db, binding))


async def test_persistent_scope_records_halt_request_but_keeps_legacy_ui_fenced(ui_session_db):
    pool = ui_session_db
    binding = await runtime(pool)
    report = await request(pool, binding)
    stopped = []

    async def halt(ticket):
        stopped.append(ticket.id)
        return {"schema": "synthetic-halt-observation"}

    with activity_context(await envelope(pool, report), uuid.UUID(report["worker_token"])):
        async with photos_ui_session(pool, native_session(binding), role="upload", halt=halt) as lease:
            lease.check()
            assert str(lease.ticket.token) not in repr(lease.ticket)
            assert await pool.fetchval("SELECT state FROM photos_ui_sessions") == "ACTIVE"
    assert stopped == [lease.ticket.id]
    assert await pool.fetchval("SELECT state FROM photos_ui_sessions") == "HALT_REQUIRED"
    assert await pool.fetchval("SELECT reason FROM photos_ui_halt_requests") == "UI_SCOPE_ENDED"
    assert await pool.fetchval("SELECT operation_id FROM photos_ui_sessions") == uuid.UUID(report["operation_id"])
    assert await pool.fetchval("SELECT generation FROM photos_ui_sessions") == report["generation"]
    with pytest.raises(CanaryBlockedError, match="lease lost"):
        lease.check()
    for account, owner in [(binding[2], uuid.uuid4()), (uuid.uuid4(), binding[1])]:
        with pytest.raises(RuntimeError, match="fenced"):
            async with photos_ui_ownership(pool, account, str(owner)):
                pytest.fail("legacy UI bypassed a persistent fence")


async def test_persistent_scope_disconnect_halts_original_writer_and_keeps_row(ui_session_db):
    pool = ui_session_db
    binding = await runtime(pool)
    report = await request(pool, binding)
    original = await envelope(pool, report)
    ready = asyncio.Event()
    stopped = []
    backend = None

    async def halt(ticket):
        stopped.append(ticket.id)
        return {}

    async def owner():
        nonlocal backend
        with activity_context(original, uuid.UUID(report["worker_token"])):
            async with photos_ui_session(pool, native_session(binding), role="upload", halt=halt) as lease:
                backend = await lease.guard.connection.fetchval("SELECT pg_backend_pid()")
                ready.set()
                await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(ready.wait(), 5)
        await pool.fetchval("SELECT pg_terminate_backend($1)", backend)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert len(stopped) == 1
        assert await pool.fetchval("SELECT state FROM photos_ui_sessions") == "ACTIVE"
        assert await pool.fetchval("SELECT count(*) FROM photos_ui_halt_requests") == 0
        with pytest.raises(RuntimeError, match="fenced"):
            async with photos_ui_ownership(pool, binding[2], str(binding[1])):
                pytest.fail("DB disconnect released a native writer")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_persistent_heartbeat_loss_waits_for_original_native_halt(ui_session_db):
    pool = ui_session_db
    binding = await runtime(pool)
    report = await request(pool, binding)
    original = await envelope(pool, report)
    ready, stopped = asyncio.Event(), asyncio.Event()

    async def halt(ticket):
        stopped.set()
        return {}

    async def owner():
        with activity_context(original, uuid.UUID(report["worker_token"])):
            async with photos_ui_session(
                pool, native_session(binding), role="upload", halt=halt, heartbeat_seconds=0.02
            ):
                ready.set()
                await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(ready.wait(), 5)
        await pool.execute("UPDATE executions SET state='WAITING_RETRY'")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert stopped.is_set()
        assert await pool.fetchval("SELECT state FROM photos_ui_sessions") == "HALT_REQUIRED"
        assert await pool.fetchval("SELECT reason FROM photos_ui_halt_requests") == "UI_GUARD_LOST"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancellation_during_halt_waits_for_stop_completion(ui_session_db):
    pool = ui_session_db
    binding = await runtime(pool)
    report = await request(pool, binding)
    original = await envelope(pool, report)
    halting, allow_stop, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def halt(ticket):
        halting.set()
        await allow_stop.wait()
        stopped.set()
        return {}

    async def owner():
        with activity_context(original, uuid.UUID(report["worker_token"])):
            async with photos_ui_session(pool, native_session(binding), role="upload", halt=halt):
                pass

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(halting.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not stopped.is_set()
        allow_stop.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert stopped.is_set()
        assert await pool.fetchval("SELECT state FROM photos_ui_sessions") == "HALT_REQUIRED"
    finally:
        allow_stop.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_worker_halt_request_is_idempotent_and_never_releases_fence(ui_session_db):
    pool = ui_session_db
    report = await request(pool, await runtime(pool))
    assert await claim(pool, report)
    body = {"id": str(uuid.uuid4()), "reason": "UI_SCOPE_ENDED", "evidence": {"schema": "synthetic"}}
    async with pool.acquire() as conn:
        await conn.execute("SET ROLE pixav_activity_worker")
        try:
            args = uuid.UUID(report["id"]), uuid.UUID(report["token"]), json.dumps(body)
            assert not await conn.fetchval(
                "SELECT request_photos_ui_halt($1,$2,$3::jsonb)", args[0], uuid.uuid4(), args[2]
            )
            assert await conn.fetchval("SELECT request_photos_ui_halt($1,$2,$3::jsonb)", *args)
            assert await conn.fetchval("SELECT request_photos_ui_halt($1,$2,$3::jsonb)", *args)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("UPDATE photos_ui_sessions SET state='STOPPED'")
        finally:
            await conn.execute("RESET ROLE")
    assert await pool.fetchval("SELECT count(*) FROM photos_ui_halt_requests") == 1
    assert await pool.fetchval("SELECT state FROM photos_ui_sessions") == "HALT_REQUIRED"


async def test_scope_cannot_return_success_after_ui_deadline_expired(ui_session_db):
    pool = ui_session_db
    binding = await runtime(pool)
    report = await request(pool, binding)
    stopped = []

    async def halt(ticket):
        stopped.append(ticket.id)
        return {}

    with activity_context(await envelope(pool, report), uuid.UUID(report["worker_token"])):
        with pytest.raises(CanaryBlockedError, match="lease lost"):
            async with photos_ui_session(pool, native_session(binding), role="upload", halt=halt) as lease:
                lease._valid_until = 0
    assert stopped == [lease.ticket.id]
    assert await pool.fetchval("SELECT reason FROM photos_ui_halt_requests") == "UI_GUARD_LOST"


async def test_halt_request_audit_and_fence_survive_full_restore(ui_session_db, tmp_path):
    from tests.integration.test_phase0_restore import postgres_tool

    pool = ui_session_db
    binding = await runtime(pool)
    report = await request(pool, binding)
    assert await claim(pool, report)
    body = {"id": str(uuid.uuid4()), "reason": "NATIVE_HALT_UNCONFIRMED", "evidence": {"schema": "synthetic"}}
    assert await pool.fetchval(
        "SELECT request_photos_ui_halt($1,$2,$3::jsonb)",
        uuid.UUID(report["id"]),
        uuid.UUID(report["token"]),
        json.dumps(body),
    )
    before = await pool.fetchval("SELECT to_jsonb(r)::text FROM photos_ui_halt_requests r")
    database = await pool.fetchval("SELECT current_database()")
    assert database.startswith("pixav_test_")
    dump = tmp_path / "ui-halt-request-full.dump"
    with create_backup_file(dump, binary=True) as handle:
        handle.write(postgres_tool("pg_dump", "-U", "pixav_test", "-Fc", "-d", database))
    postgres_tool(
        "pg_restore",
        "-U",
        "pixav_test",
        "--clean",
        "--if-exists",
        "--exit-on-error",
        "-d",
        database,
        payload=dump.read_bytes(),
    )
    assert before == await pool.fetchval("SELECT to_jsonb(r)::text FROM photos_ui_halt_requests r")
    assert await pool.fetchval("SELECT state FROM photos_ui_sessions") == "HALT_REQUIRED"
    assert not await claim(pool, await request(pool, binding))
