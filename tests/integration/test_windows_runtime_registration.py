"""Enrollment uses real PostgreSQL; native/Docker adapters are contract fakes."""

import json
import uuid
from types import SimpleNamespace

import asyncpg
import pytest

from pixav.pixel_injector.windows_registration import RegistrationError
from pixav.shared.activity_context import activity_context
from tests.integration import test_windows_managed_runtime as runtime_contract
from tests.integration.test_external_android_runtime import claim, envelope, request

managed = runtime_contract.managed

pytestmark = [pytest.mark.integration, pytest.mark.parametrize("managed", [False], indirect=True)]


async def fingerprint(pool):
    return await pool.fetchval("""SELECT jsonb_build_object(
       'runtime',(SELECT jsonb_agg(to_jsonb(r) ORDER BY runtime_id,generation) FROM upload_runtimes r),
       'audit',(SELECT jsonb_agg(to_jsonb(e) ORDER BY runtime_id,generation) FROM windows_runtime_enrollments e),
       'execution',(SELECT jsonb_agg(to_jsonb(x) ORDER BY id) FROM executions x),
       'ui',(SELECT jsonb_agg(to_jsonb(s) ORDER BY id) FROM photos_ui_sessions s))::text""")


async def enroll(state):
    return await state.registry.enroll(
        state.spec, apply=True, operator="synthetic-operator", reason="isolated contract"
    )


async def test_dry_run_changes_no_rows_or_host_journal(managed):
    state = managed
    before = await fingerprint(state.pool)
    assert (await state.registry.enroll(state.spec))["registry_rows_written"] == 0
    assert await fingerprint(state.pool) == before
    assert not state.host.journal.exists()
    state.runner.start.assert_not_called()
    state.runner.stop.assert_not_called()
    assert state.calls == ["quiescence"]


async def test_apply_is_atomic_audited_and_idempotent(managed):
    state = managed
    result = await enroll(state)
    assert result["state"] == "READY" and not result["already_registered"]
    before = await fingerprint(state.pool)
    result = await enroll(state)
    assert result["already_registered"] and await fingerprint(state.pool) == before
    assert await state.pool.fetchval("SELECT count(*) FROM windows_runtime_enrollments") == 1
    assert await state.pool.fetchval("SELECT count(*) FROM photos_ui_sessions") == 0
    assert (
        await state.pool.fetchval("SELECT native_receipt->>'identity_sha256' FROM windows_runtime_enrollments")
        == state.spec.host.identity_sha256
    )


@pytest.mark.parametrize("change", ["daemon", "script", "staging"])
async def test_existing_enrollment_cannot_be_reused_for_changed_binding(managed, change):
    state = managed
    await enroll(state)
    if change == "daemon":
        state.spec = state.spec.model_copy(update={"docker_daemon_id": "new-daemon"})
        state.docker.info.return_value = {"ID": "new-daemon"}
    elif change == "script":
        state.spec = state.spec.model_copy(update={"native_script_sha256": "f" * 64})
        state.host.control.script_sha256 = "f" * 64
    else:
        root = state.runtime.staging_root
        root.rename(root.with_name("previous-staging"))
        root.mkdir()
        stat = root.stat()
        state.spec = state.spec.model_copy(update={"staging_device": stat.st_dev, "staging_inode": stat.st_ino})
        state.runner.exec_run.return_value = SimpleNamespace(
            exit_code=0, output=json.dumps({"device": stat.st_dev, "inode": stat.st_ino}).encode()
        )
    with pytest.raises(RegistrationError, match="ENROLLMENT_BINDING_CHANGED"):
        await enroll(state)


async def test_another_account_or_device_alias_cannot_bypass_same_physical_avd(managed):
    state = managed
    await enroll(state)
    account = uuid.uuid4()
    await state.pool.execute("INSERT INTO accounts(id,email) VALUES($1,'alias@example.invalid')", account)
    alias = state.spec.model_copy(
        update={
            "account_id": account,
            "owner": uuid.uuid4(),
            "host": state.spec.host.model_copy(update={"runtime_id": uuid.uuid4(), "device_key": "synthetic-alias"}),
        }
    )
    calls_before = len(state.calls)
    with pytest.raises(RegistrationError, match="REQUIRES_RETIREMENT"):
        await state.registry.enroll(alias, apply=True, operator="synthetic", reason="must reject alias")
    assert len(state.calls) == calls_before
    assert await state.pool.fetchval("SELECT count(*) FROM upload_runtimes") == 1


@pytest.mark.parametrize("field", ["db_identity", "database"])
async def test_wrong_instance_rejected_before_external_observation(managed, field):
    state = managed
    setattr(state.registry, field, "different-instance")
    with pytest.raises(RegistrationError, match="DATABASE_IDENTITY_CHANGED"):
        await enroll(state)
    assert not state.calls
    state.docker.containers.get.assert_not_called()
    assert await state.pool.fetchval("SELECT count(*) FROM upload_runtimes") == 0


@pytest.mark.parametrize(
    "defect", ["daemon", "socket", "writable", "privileged", "physical", "container-inode", "image"]
)
async def test_unverified_tools_or_mounts_cannot_be_enrolled(managed, defect):
    state = managed
    if defect == "daemon":
        state.docker.info.return_value = {"ID": "different-daemon"}
    elif defect == "socket":
        state.runner.attrs["Mounts"].append(
            {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True}
        )
    elif defect == "writable":
        state.runner.attrs["Mounts"][0]["RW"] = True
    elif defect == "privileged":
        state.runner.attrs["HostConfig"]["Privileged"] = True
    elif defect == "physical":
        state.spec = state.spec.model_copy(update={"staging_inode": state.spec.staging_inode + 1})
    elif defect == "container-inode":
        state.runner.exec_run.return_value = SimpleNamespace(exit_code=0, output=b'{"device":0,"inode":1}')
    elif defect == "image":
        state.runner.attrs["Config"]["Image"] = "mutable-tools:latest"
    before = await fingerprint(state.pool)
    with pytest.raises(RegistrationError):
        await enroll(state)
    assert await fingerprint(state.pool) == before and not state.calls


async def test_failure_after_native_readiness_keeps_registry_empty(managed):
    state = managed
    good = state.runner.exec_run.return_value
    state.runner.exec_run.side_effect = [good, SimpleNamespace(exit_code=1, output=b"")]
    with pytest.raises(RegistrationError, match="PROBE_UNCONFIRMED"):
        await enroll(state)
    assert await state.pool.fetchval("SELECT count(*) FROM upload_runtimes") == 0
    assert await state.pool.fetchval("SELECT count(*) FROM windows_runtime_enrollments") == 0
    assert state.calls == ["quiescence"]


async def test_failed_ready_commit_rolls_back_both_registry_and_audit(managed):
    state = managed
    await state.pool.execute("""CREATE FUNCTION reject_ready_fixture() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF NEW.state='READY' THEN RAISE EXCEPTION 'synthetic commit failure'; END IF; RETURN NEW; END $$;
      CREATE TRIGGER reject_ready_fixture BEFORE UPDATE ON upload_runtimes
        FOR EACH ROW EXECUTE FUNCTION reject_ready_fixture()""")
    with pytest.raises(asyncpg.RaiseError):
        await enroll(state)
    assert await state.pool.fetchval("SELECT count(*) FROM upload_runtimes") == 0
    assert await state.pool.fetchval("SELECT count(*) FROM windows_runtime_enrollments") == 0


async def test_active_and_expired_ui_ownership_are_never_bypassed(managed):
    state = managed
    await enroll(state)
    report = await request(state.pool, state.binding, role="observe")
    with activity_context(await envelope(state.pool, report), uuid.UUID(report["worker_token"])):
        async with state.runtime.acquire(state.binding[2], role="observe"):
            with pytest.raises(RegistrationError, match="UNRECONCILED"):
                await enroll(state)
    await state.pool.execute("UPDATE photos_ui_sessions SET lease_until=clock_timestamp()-interval '1 hour'")
    before = await fingerprint(state.pool)
    with pytest.raises(RegistrationError, match="UNRECONCILED"):
        await enroll(state)
    assert await fingerprint(state.pool) == before


async def test_unreconciled_physical_owner_survives_review_state_and_alias(managed):
    state = managed
    await enroll(state)
    account = uuid.uuid4()
    await state.pool.execute("INSERT INTO accounts(id,email) VALUES($1,'review-alias@example.invalid')", account)
    alias = state.spec.model_copy(
        update={
            "account_id": account,
            "owner": uuid.uuid4(),
            "host": state.spec.host.model_copy(update={"runtime_id": uuid.uuid4(), "device_key": "review-alias"}),
        }
    )
    report = await request(state.pool, state.binding, role="observe")
    with activity_context(await envelope(state.pool, report), uuid.UUID(report["worker_token"])):
        async with state.runtime.acquire(state.binding[2], role="observe"):
            await state.pool.execute(
                "UPDATE upload_runtimes SET state='REVIEW_REQUIRED',review_reason='synthetic review'"
            )
            before = len(state.calls)
            with pytest.raises(RegistrationError, match="UNRECONCILED"):
                await state.registry.enroll(alias, apply=True, operator="synthetic", reason="reject review alias")
            assert len(state.calls) == before
    assert await state.pool.fetchval("SELECT count(*) FROM upload_runtimes") == 1


async def test_ready_requires_audit_and_audit_is_immutable(managed):
    state = managed
    await state.pool.execute(
        """INSERT INTO upload_runtimes(runtime_id,generation,owner,kind,account_id,device_key,profile,
      identity,identity_sha256,tools_id,tools_image,staging_root,state)
      VALUES($1,$2,$3,'windows_android',$4,$5,$6,$7::jsonb,$8,$9,$10,$11,'REGISTERED')""",
        *state.spec.runtime_values(),
    )
    with pytest.raises(asyncpg.CheckViolationError, match="verified enrollment"):
        await state.pool.execute("UPDATE upload_runtimes SET state='READY'")
    await enroll(state)
    for query in ["UPDATE windows_runtime_enrollments SET reason='altered'", "DELETE FROM windows_runtime_enrollments"]:
        with pytest.raises(asyncpg.CheckViolationError, match="immutable"):
            await state.pool.execute(query)
    async with state.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE pixav_activity_worker")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("INSERT INTO windows_runtime_enrollments SELECT * FROM windows_runtime_enrollments")


async def test_full_restore_keeps_enrollment_and_original_ui_operation(managed):
    from tests.integration.test_phase0_restore import postgres_tool

    state = managed
    await enroll(state)
    report = await request(state.pool, state.binding)
    assert await claim(state.pool, report)
    before = await fingerprint(state.pool)
    database = await state.pool.fetchval("SELECT current_database()")
    assert database.startswith("pixav_test_")
    # Disposable, synthetic test-database bytes only; no operator backup job.
    dump = postgres_tool("pg_dump", "-U", "pixav_test", "-Fc", "-d", database)
    postgres_tool(
        "pg_restore", "-U", "pixav_test", "--clean", "--if-exists", "--exit-on-error", "-d", database, payload=dump
    )
    assert await fingerprint(state.pool) == before
    with pytest.raises(RegistrationError, match="UNRECONCILED"):
        await enroll(state)
