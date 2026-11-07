"""Real PostgreSQL account leasing, exactly-once quota and durability commits.

Every assertion here is about pixAV's own bookkeeping. The provider is
represented by reported activity results; nothing here demonstrates that a real
Google Photos upload or cold read-back succeeded.
"""

import asyncio
import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from pixav.maxwell_core.media_workflow import MediaWorkflow
from pixav.maxwell_core.storage_workflow import StorageWorkflow
from pixav.media_loader.preparation import MediaFacts, PreparedArtifact
from pixav.shared.workflow import ActivityResult
from pixav.sht_probe.policy import SourcePolicy

pytestmark = pytest.mark.integration

SHARE = "https://photos.app.goo.gl/synthetic"
BACKUP = {"backed_up": True, "original_quality": True}
SIZE = 2048
DIGEST = "c" * 64

# What the prepared artifact is. The asset records this at creation, and a cold
# read-back has to report seeing the same media, not merely the same bytes.
FACTS = {
    "container": "mov,mp4",
    "size_bytes": SIZE,
    "duration_seconds": 10.0,
    "sha256": DIGEST,
    "streams": [
        {"kind": "video", "codec": "h264", "width": 1920, "height": 1080},
        {"kind": "audio", "codec": "aac", "width": 0, "height": 0},
    ],
}


def cold_receipt(**changes):
    """A receipt a cold read-back container would produce under photos-original-v2."""
    return {
        "method": "photos-original-browser",
        "size": SIZE,
        "sha256": DIGEST,
        "cold_inputs": "provider-only",
        "observed": dict(FACTS),
        **changes,
    }


@pytest.fixture
async def workflow(integration_db):
    for path in sorted(Path("migrations").glob("*.sql")):
        await integration_db.execute(path.read_text())
    return MediaWorkflow(integration_db, backoff=(1, 2), lease_seconds=120)


def report(request, outcome="success", **kwargs):
    fields = request.model_dump(exclude={"stage", "identity", "input_path", "share_url"})
    return ActivityResult(**fields, outcome=outcome, **kwargs)


async def submit(workflow, result):
    return await workflow.pool.fetchval("SELECT report_activity($1::jsonb)", result.model_dump_json())


async def add_account(workflow, *, email=None, **columns):
    account_id = uuid.uuid4()
    await workflow.pool.execute(
        "INSERT INTO accounts(id,email) VALUES($1,$2)", account_id, email or f"{account_id}@example.invalid"
    )
    for column, value in columns.items():
        # Column names come from this module's own keyword arguments, never input.
        await workflow.pool.execute(
            f"UPDATE accounts SET {column}=$2 WHERE id=$1",  # noqa: S608
            account_id,
            value,
        )
    return account_id


@pytest.fixture
async def held_upload_review(workflow):
    from pixav.shared.remote_assets import segment_from_row
    from pixav.shared.upload_outcome_review import recovery_digest

    await reach_upload(workflow)
    account = await add_account(workflow)
    upload = await workflow.next_activity()
    recovery = {"push_intent": True, "publish_intent": True}
    await workflow.pool.execute(
        "UPDATE remote_asset_segments SET recovery=$2::jsonb WHERE asset_id=$1", upload.asset_id, json.dumps(recovery)
    )
    await submit(workflow, report(upload, "user_action", error_code="PUBLISHED_COPY_HASH_MISMATCH"))
    await workflow.consume_results()
    owner = uuid.uuid4()
    await workflow.pool.execute(
        """INSERT INTO guest_runtimes(owner,state,account_id,profile,guest_image,tools_image,guest_id,tools_id,staging_root)
        VALUES($1,'READY',$2,'gphotos_pixel_xl_v1','synthetic-guest-image','synthetic-tools-image','guest','tools','/synthetic')""",
        owner,
        account,
    )
    now = await workflow.pool.fetchval("SELECT clock_timestamp()")
    execution = await workflow.pool.fetchrow("SELECT * FROM executions WHERE id=$1", upload.execution_id)
    segment = segment_from_row(
        await workflow.pool.fetchrow("SELECT * FROM remote_asset_segments WHERE asset_id=$1", upload.asset_id)
    )
    evidence = {
        "version": "upload-outcome-review-v1",
        "outcome": "EXISTING_ORIGINAL",
        "database_identity": str(await workflow.pool.fetchval("SELECT system_identifier FROM pg_control_system()")),
        "database": await workflow.pool.fetchval("SELECT current_database()"),
        "account_id": str(account),
        "guest_owner": str(owner),
        "profile": "gphotos_pixel_xl_v1",
        "execution_id": str(upload.execution_id),
        "operation_id": str(upload.operation_id),
        "asset_id": str(upload.asset_id),
        "segment_index": 0,
        "generation": execution["generation"],
        "segment_sha256": DIGEST,
        "recovery_sha256": recovery_digest(segment.recovery),
        "artifact_sha256": "f" * 64,
        "observed_at": now.isoformat(),
        "provider_completed_after": (now - timedelta(minutes=10)).isoformat(),
        "provider_completed_before": now.isoformat(),
        "share_url": SHARE,
        "backup_evidence": BACKUP,
        "cold_readback": cold_receipt(
            provider_asset_id=str(upload.asset_id),
            provider_segment_index=0,
            provider_share_sha256=hashlib.sha256(SHARE.encode()).hexdigest(),
            cold_mounts=["/work:rw"],
            at=now.isoformat(),
        ),
    }
    return upload, account, evidence, recovery


async def test_original_upload_review_queues_only_then_authority_preserves_operation_and_usage(
    workflow, held_upload_review
):
    upload, account, evidence, recovery = held_upload_review
    audit = {"operator": "fixture", "reason": "exact original independently recovered"}
    before = await workflow.pool.fetchval("SELECT checkpoint FROM executions WHERE id=$1", upload.execution_id)
    assert await workflow.storage.review_upload_outcome(upload.execution_id, 0, evidence, **audit)
    assert await workflow.pool.fetchval("SELECT checkpoint FROM executions WHERE id=$1", upload.execution_id) == before
    assert await workflow.storage.review_upload_outcome(upload.execution_id, 0, evidence, **audit, apply=True)
    assert not await workflow.storage.review_upload_outcome(upload.execution_id, 0, evidence, **audit, apply=True)
    assert (
        await workflow.pool.fetchval("SELECT state FROM executions WHERE id=$1", upload.execution_id)
        == "USER_ACTION_REQUIRED"
    )
    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == 0
    assert await workflow.storage.consume_upload_reviews() == 1
    assert await workflow.storage.consume_upload_reviews() == 0
    next_request = await workflow.next_activity()
    assert next_request.operation_id == upload.operation_id
    assert next_request.account_id == account
    saved = json.loads(
        await workflow.pool.fetchval("SELECT recovery FROM remote_asset_segments WHERE asset_id=$1", upload.asset_id)
    )
    assert all(saved[k] == v for k, v in recovery.items())
    result = report(next_request, share_url=SHARE, evidence=BACKUP)
    assert await submit(workflow, result)
    assert await workflow.consume_results() == 1
    assert await workflow.consume_results() == 0
    assert not await submit(workflow, result)
    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == SIZE
    assert not await workflow.storage.review_upload_outcome(upload.execution_id, 0, evidence, **audit, apply=True)
    assert await workflow.pool.fetchval("SELECT count(*) FROM operation_intents WHERE stage='upload'") == 1


@pytest.mark.parametrize(
    "defect",
    [
        "account_id",
        "operation_id",
        "asset_id",
        "segment_sha256",
        "recovery_sha256",
        "generation",
        "stale",
        "hash",
        "mounts",
        "guest_owner",
        "database",
    ],
)
async def test_original_upload_review_refuses_changed_or_unproven_evidence(workflow, held_upload_review, defect):
    upload, account, evidence, _ = held_upload_review
    if defect in ("account_id", "operation_id", "asset_id", "guest_owner"):
        evidence[defect] = str(uuid.uuid4())
    elif defect in ("segment_sha256", "recovery_sha256"):
        evidence[defect] = "0" * 64
    elif defect == "generation":
        evidence[defect] -= 1
    elif defect == "stale":
        evidence["observed_at"] = (
            await workflow.pool.fetchval("SELECT clock_timestamp()") - timedelta(hours=2)
        ).isoformat()
        evidence["provider_completed_before"] = evidence["observed_at"]
        evidence["provider_completed_after"] = evidence["observed_at"]
    elif defect == "hash":
        evidence["cold_readback"]["sha256"] = "0" * 64
    elif defect == "mounts":
        evidence["cold_readback"]["cold_mounts"].append("/staging:ro")
    elif defect == "database":
        evidence["database"] = "wrong-database"
    with pytest.raises(ValueError):
        await workflow.storage.review_upload_outcome(
            upload.execution_id, 0, evidence, operator="fixture", reason="review", apply=True
        )
    assert (
        await workflow.pool.fetchval("SELECT state FROM executions WHERE id=$1", upload.execution_id)
        == "USER_ACTION_REQUIRED"
    )
    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == 0


async def test_pending_review_cannot_release_changed_recovery(workflow, held_upload_review):
    upload, _, evidence, recovery = held_upload_review
    await workflow.storage.review_upload_outcome(
        upload.execution_id, 0, evidence, operator="fixture", reason="review", apply=True
    )
    await workflow.pool.execute(
        "UPDATE remote_asset_segments SET recovery=$2::jsonb WHERE asset_id=$1",
        upload.asset_id,
        json.dumps({**recovery, "share_intent": True}),
    )
    assert await workflow.storage.consume_upload_reviews() == 0
    assert (
        await workflow.pool.fetchval("SELECT state FROM executions WHERE id=$1", upload.execution_id)
        == "USER_ACTION_REQUIRED"
    )
    checkpoint = json.loads(
        await workflow.pool.fetchval("SELECT checkpoint FROM executions WHERE id=$1", upload.execution_id)
    )
    assert checkpoint["upload_outcome_review"]["status"] == "REJECTED"


async def test_review_success_crash_before_commit_keeps_usage_and_result_reentrant(
    workflow, held_upload_review, monkeypatch
):
    upload, account, evidence, _ = held_upload_review
    await workflow.storage.review_upload_outcome(
        upload.execution_id, 0, evidence, operator="fixture", reason="review", apply=True
    )
    await workflow.storage.consume_upload_reviews()
    next_request = await workflow.next_activity()
    await submit(workflow, report(next_request, share_url=SHARE, evidence=BACKUP))
    original = workflow.storage.assets.confirm_backup

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("synthetic crash after debit, before transaction commit")

    monkeypatch.setattr(workflow.storage.assets, "confirm_backup", crash)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        await workflow.consume_results()
    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == 0
    assert (
        await workflow.pool.fetchval(
            "SELECT usage_counted_at FROM remote_asset_segments WHERE asset_id=$1", upload.asset_id
        )
        is None
    )
    assert (
        await workflow.pool.fetchval(
            "SELECT consumed_at FROM activity_results WHERE attempt_id=$1", next_request.attempt_id
        )
        is None
    )
    monkeypatch.setattr(workflow.storage.assets, "confirm_backup", original)
    assert await workflow.consume_results() == 1
    assert await workflow.consume_results() == 0
    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == SIZE


async def test_review_cross_day_confirmation_debits_confirmation_day_once(workflow, held_upload_review):
    upload, account, evidence, _ = held_upload_review
    await workflow.pool.execute(
        "UPDATE accounts SET daily_uploaded_bytes=123,quota_reset_at=now()-interval '1 day' WHERE id=$1", account
    )
    await workflow.storage.review_upload_outcome(
        upload.execution_id, 0, evidence, operator="fixture", reason="review", apply=True
    )
    await workflow.storage.consume_upload_reviews()
    next_request = await workflow.next_activity()
    result = report(next_request, share_url=SHARE, evidence=BACKUP)
    await submit(workflow, result)
    await workflow.consume_results()
    await workflow.consume_results()
    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == SIZE
    assert not await submit(workflow, result)


async def reach_upload(workflow, info_hash="a" * 40):
    """Drive one video from discovery to the point where storage takes over."""
    video = await workflow.pool.fetchval("INSERT INTO videos(title) VALUES('synthetic fixture') RETURNING id")
    candidates, _ = SourcePolicy().normalize_batch(
        [
            {
                "provider": "synthetic",
                "provider_id": "one",
                "title": "synthetic 1080p .mp4",
                "magnet_uri": "magnet:?xt=urn:btih:" + info_hash,
            }
        ]
    )
    await workflow.observe(video, candidates)
    task = await workflow.admit(video, max_retries=1)
    download = await workflow.next_activity()
    await submit(workflow, report(download, artifact_path="/staging/synthetic.mkv"))
    await workflow.consume_results()
    prepare = await workflow.next_activity()
    facts = MediaFacts(
        container="mov,mp4",
        size_bytes=SIZE,
        duration_seconds=10,
        sha256=DIGEST,
        streams=({"kind": "video", "codec": "h264", "width": 1920, "height": 1080}, {"kind": "audio", "codec": "aac"}),
    )
    artifact = PreparedArtifact(
        path="/staging/prepared.mp4",
        input_path=prepare.input_path,
        input_sha256="b" * 64,
        facts=facts,
        input_facts=facts.model_copy(update={"container": "matroska", "sha256": "b" * 64}),
    )
    await submit(workflow, report(prepare, prepared=artifact))
    await workflow.consume_results()
    return video, task


async def test_prepared_artifact_becomes_a_requested_remote_asset_bdd_046(workflow):
    video, _ = await reach_upload(workflow)
    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets WHERE video_id=$1", video)
    assert asset["provider"] == "google_photos"
    assert asset["state"] == "REQUESTED"
    assert asset["segment_count"] == 1
    assert await workflow.pool.fetchval("SELECT count(*) FROM tasks WHERE queue_name='pixav:upload'") == 0
    assert await workflow.pool.fetchval("SELECT stage FROM executions") == "upload"


async def test_eligible_account_is_leased_to_the_execution_bdd_036_040(workflow):
    await reach_upload(workflow)
    account = await add_account(workflow)
    request = await workflow.next_activity()
    assert request.stage == "upload"
    assert request.account_id == account
    lease = await workflow.pool.fetchrow("SELECT * FROM account_leases")
    assert lease["account_id"] == account
    assert lease["execution_id"] == request.execution_id
    assert lease["owner"] == request.owner


async def test_exhausted_and_cooling_accounts_are_not_selected_bdd_037_038_039(workflow):
    await reach_upload(workflow)
    await add_account(workflow, daily_uploaded_bytes=10**9, daily_quota_bytes=10**9)
    await workflow.pool.execute(
        "UPDATE accounts SET cooldown_until=now()+interval '1 hour' WHERE daily_uploaded_bytes=0"
    )
    await add_account(workflow)
    await workflow.pool.execute(
        "UPDATE accounts SET cooldown_until=now()+interval '1 hour' WHERE cooldown_until IS NULL"
    )

    assert await workflow.next_activity() is None

    execution = await workflow.pool.fetchrow("SELECT * FROM executions")
    assert execution["state"] == "WAITING_QUOTA"
    assert execution["blocked_reason"] == "WAITING_QUOTA"
    assert await workflow.pool.fetchval("SELECT count(*) FROM account_leases") == 0
    # The media a wait depends on must still be there when the wait ends.
    assert await workflow.pool.fetchval("SELECT local_path FROM videos") == "/staging/prepared.mp4"


async def test_quota_wait_resumes_on_database_time_bdd_051_006(workflow):
    await reach_upload(workflow)
    await add_account(workflow, daily_uploaded_bytes=10**9, daily_quota_bytes=10**9)
    assert await workflow.next_activity() is None
    assert await workflow.pool.fetchval("SELECT state FROM executions") == "WAITING_QUOTA"

    # Only the clock moves. Nothing resets the segment by hand, so resumption
    # has to come from the quota reset itself.
    await workflow.pool.execute("UPDATE accounts SET quota_reset_at=now()-interval '1 second'")
    await workflow.pool.execute("UPDATE executions SET due_at=now()-interval '1 second'")

    request = await workflow.next_activity()
    assert request is not None and request.stage == "upload"


async def test_upload_success_charges_quota_exactly_once_bdd_049_113(workflow):
    await reach_upload(workflow)
    account = await add_account(workflow)
    request = await workflow.next_activity()
    result = report(request, share_url=SHARE, evidence=BACKUP)

    assert await submit(workflow, result)
    assert await workflow.consume_results() == 1
    # A duplicate report is inert, and a duplicate consume must not re-debit.
    assert not await submit(workflow, result)
    assert await workflow.consume_results() == 0

    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == SIZE
    segment = await workflow.pool.fetchrow("SELECT * FROM remote_asset_segments")
    assert segment["state"] == "backed_up" and segment["share_url"] == SHARE
    assert segment["usage_counted_at"] is not None


async def test_remote_creation_alone_is_not_durable_bdd_047(workflow):
    await reach_upload(workflow)
    await add_account(workflow)
    request = await workflow.next_activity()
    await submit(workflow, report(request, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()

    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets")
    assert asset["state"] == "CREATED"
    assert asset["durable_at"] is None


async def test_successful_upload_releases_the_lease_bdd_041(workflow):
    await reach_upload(workflow)
    await add_account(workflow)
    request = await workflow.next_activity()
    await submit(workflow, report(request, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()
    assert await workflow.pool.fetchval("SELECT count(*) FROM account_leases") == 0


async def test_expired_lease_without_heartbeat_frees_the_account_bdd_042(workflow):
    await reach_upload(workflow)
    account = await add_account(workflow)
    first = await workflow.next_activity()
    assert await workflow.pool.fetchval("SELECT count(*) FROM account_leases") == 1

    await workflow.pool.execute("UPDATE executions SET lease_until=now()-interval '1 second'")
    await workflow.pool.execute("UPDATE account_leases SET lease_until=now()-interval '1 second'")
    await workflow.recover_expired()

    assert await workflow.pool.fetchval("SELECT count(*) FROM account_leases") == 0
    second = await workflow.next_activity()
    assert second.attempt_id != first.attempt_id
    assert second.account_id == account


async def test_failed_upload_is_not_charged_as_usage_bdd_050(workflow):
    await reach_upload(workflow)
    account = await add_account(workflow)
    request = await workflow.next_activity()
    await submit(workflow, report(request, "unknown_effect", error_code="REMOTE_EFFECT_UNKNOWN"))
    await workflow.consume_results()

    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == 0
    assert await workflow.pool.fetchval("SELECT usage_counted_at FROM remote_asset_segments") is None


async def test_login_challenge_stops_and_waits_for_an_operator_bdd_045(workflow):
    await reach_upload(workflow)
    await add_account(workflow)
    request = await workflow.next_activity()
    await submit(workflow, report(request, "user_action", error_code="USER_ACTION_REQUIRED"))
    await workflow.consume_results()

    assert await workflow.pool.fetchval("SELECT state FROM executions") == "USER_ACTION_REQUIRED"
    assert await workflow.pool.fetchval("SELECT state FROM remote_asset_segments") == "user_action_required"
    assert await workflow.pool.fetchval("SELECT count(*) FROM account_leases") == 0
    # Nothing is dispatched again on its own; an operator has to act.
    assert await workflow.next_activity() is None


async def test_an_operator_resume_releases_the_held_execution_bdd_045(workflow):
    """Only a person who looked at the device may release the hold."""
    await reach_upload(workflow)
    await add_account(workflow)
    request = await workflow.next_activity()
    await workflow.pool.execute(
        """UPDATE remote_asset_segments SET recovery=$1::jsonb""",
        '{"email_submitted": true, "operations": {"credential_email": {"intent_at": "t"}}}',
    )
    await submit(workflow, report(request, "user_action", error_code="USER_ACTION_REQUIRED"))
    await workflow.consume_results()

    storage = StorageWorkflow(workflow.pool)
    await storage.resume_after_user_action(
        await workflow.pool.fetchval("SELECT id FROM executions"),
        operator="fixture-operator",
        reason="device inspected; the address never reached the page",
        cleared=("email_submitted", "credential_email"),
    )

    assert await workflow.pool.fetchval("SELECT state FROM executions") == "READY"
    assert await workflow.pool.fetchval("SELECT state FROM remote_asset_segments") == "prepared"
    recovery = json.loads(await workflow.pool.fetchval("SELECT recovery FROM remote_asset_segments"))
    assert "email_submitted" not in recovery and recovery["operations"] == {}
    checkpoint = json.loads(await workflow.pool.fetchval("SELECT checkpoint FROM executions"))
    assert checkpoint["user_action_resume"]["operator"] == "fixture-operator"
    # The authority now has something to dispatch again.
    assert (await workflow.next_activity()).stage == "upload"


async def test_a_resume_refuses_to_clear_a_remote_effect_bdd_045(workflow):
    """Clearing a push or share receipt would order the effect redone blindly."""
    await reach_upload(workflow)
    await add_account(workflow)
    request = await workflow.next_activity()
    await submit(workflow, report(request, "user_action", error_code="USER_ACTION_REQUIRED"))
    await workflow.consume_results()
    execution = await workflow.pool.fetchval("SELECT id FROM executions")

    storage = StorageWorkflow(workflow.pool)
    with pytest.raises(ValueError, match="remote effects"):
        await storage.resume_after_user_action(
            execution, operator="fixture-operator", reason="tempted", cleared=("push",)
        )
    with pytest.raises(ValueError, match="operator and reason"):
        await storage.resume_after_user_action(execution, operator="", reason="")

    assert await workflow.pool.fetchval("SELECT state FROM executions") == "USER_ACTION_REQUIRED"


async def test_only_a_held_execution_may_be_resumed_bdd_045(workflow):
    await reach_upload(workflow)
    await add_account(workflow)
    execution = await workflow.pool.fetchval("SELECT id FROM executions")

    with pytest.raises(ValueError, match="held for user action"):
        await StorageWorkflow(workflow.pool).resume_after_user_action(
            execution, operator="fixture-operator", reason="not held"
        )


async def test_storage_resume_cannot_release_a_held_playback_execution(workflow):
    await reach_upload(workflow)
    execution = await workflow.pool.fetchval("SELECT id FROM executions")
    await workflow.pool.execute("UPDATE executions SET state='USER_ACTION_REQUIRED',stage='playback'")
    with pytest.raises(ValueError, match="another execution stage"):
        await StorageWorkflow(workflow.pool).resume_after_user_action(
            execution, operator="fixture-operator", reason="wrong resume entry"
        )
    assert await workflow.pool.fetchval("SELECT state FROM executions") == "USER_ACTION_REQUIRED"


async def test_a_held_upload_is_not_dispatched_until_resumed(workflow):
    """An operator can park one upload without the system pause or an account cooldown."""
    from pixav.maxwell_core.upload_hold import hold_upload

    await reach_upload(workflow)
    await add_account(workflow)
    execution = await workflow.pool.fetchval("SELECT id FROM executions")
    generation = await workflow.pool.fetchval("SELECT generation FROM executions")

    await hold_upload(workflow.pool, execution, operator="fixture-operator", reason="another upload is verifying")

    row = await workflow.pool.fetchrow("SELECT state,blocked_reason,generation,checkpoint FROM executions")
    assert (row["state"], row["blocked_reason"]) == ("USER_ACTION_REQUIRED", "OPERATOR_UPLOAD_HOLD")
    assert row["generation"] == generation + 1
    assert json.loads(row["checkpoint"])["operator_upload_hold"]["state_before"] == "READY"
    assert await workflow.pool.fetchval("SELECT state FROM remote_asset_segments") == "prepared"
    assert await workflow.next_activity() is None
    assert await workflow.pool.fetchval("SELECT count(*) FROM account_leases") == 0

    await StorageWorkflow(workflow.pool).resume_after_user_action(
        execution, operator="fixture-operator", reason="verification finished"
    )
    assert (await workflow.next_activity()).stage == "upload"


async def test_a_running_or_non_upload_execution_cannot_be_held(workflow):
    from pixav.maxwell_core.upload_hold import hold_upload

    await reach_upload(workflow)
    await add_account(workflow)
    execution = await workflow.pool.fetchval("SELECT id FROM executions")
    await workflow.next_activity()
    assert await workflow.pool.fetchval("SELECT state FROM executions") == "RUNNING"
    with pytest.raises(ValueError, match="not running"):
        await hold_upload(workflow.pool, execution, operator="fixture-operator", reason="too late")
    with pytest.raises(ValueError, match="operator and reason"):
        await hold_upload(workflow.pool, execution, operator=" ", reason="")
    await workflow.pool.execute("UPDATE executions SET state='READY',stage='verify'")
    with pytest.raises(ValueError, match="only an upload"):
        await hold_upload(workflow.pool, execution, operator="fixture-operator", reason="wrong stage")
    assert await workflow.pool.fetchval("SELECT count(*) FROM account_leases") == 1


async def test_verification_failure_never_becomes_durable_bdd_055_059(workflow):
    await reach_upload(workflow)
    await add_account(workflow)
    upload = await workflow.next_activity()
    await submit(workflow, report(upload, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()
    verify = await workflow.next_activity()
    assert verify.stage == "verify" and verify.share_url == SHARE

    await submit(workflow, report(verify, "verification_failed", error_code="INTEGRITY_MISMATCH"))
    await workflow.consume_results()

    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets")
    assert asset["state"] == "CREATED" and asset["durable_at"] is None
    assert await workflow.pool.fetchval("SELECT local_path FROM videos") == "/staging/prepared.mp4"


async def test_verified_cold_readback_commits_durability_bdd_052_054_056(workflow):
    await reach_upload(workflow)
    await add_account(workflow)
    upload = await workflow.next_activity()
    await submit(workflow, report(upload, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()
    verify = await workflow.next_activity()
    receipt = cold_receipt()
    await submit(workflow, report(verify, evidence=receipt))
    await workflow.consume_results()

    assert await workflow.next_activity() is None
    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets")
    assert asset["state"] == "DURABLE" and asset["durable_at"] is not None
    assert await workflow.pool.fetchval("SELECT state FROM executions") == "SUCCEEDED"
    retention = await workflow.pool.fetchval("SELECT local_cleanup_after FROM videos")
    from datetime import timedelta

    assert retention == asset["durable_at"] + timedelta(hours=24)


@pytest.mark.parametrize("case", ["configured", "longer", "local"])
async def test_durable_retention_uses_db_clock_preserves_longer_deadlines_and_local_files_bdd_006(workflow, case):
    from datetime import timedelta

    video, _ = await reach_upload(workflow)
    workflow.storage = StorageWorkflow(workflow.pool, local_retention_hours=37)
    prior = None
    if case == "longer":
        prior = await workflow.pool.fetchval(
            "UPDATE videos SET local_cleanup_after=now()+interval '7 days' WHERE id=$1 RETURNING local_cleanup_after",
            video,
        )
    elif case == "local":
        await workflow.pool.execute("UPDATE videos SET share_url='pixav-local://synthetic' WHERE id=$1", video)
    await add_account(workflow)
    upload = await workflow.next_activity()
    await submit(workflow, report(upload, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()
    assert await workflow.pool.fetchval("SELECT local_cleanup_after FROM videos") == prior
    verify = await workflow.next_activity()
    await submit(workflow, report(verify, evidence=cold_receipt()))
    await workflow.consume_results()
    assert await workflow.next_activity() is None

    asset = await workflow.pool.fetchrow("SELECT durable_at FROM remote_assets")
    expected = asset["durable_at"] + timedelta(hours=37) if case == "configured" else prior
    assert await workflow.pool.fetchval("SELECT local_cleanup_after FROM videos") == expected

    # A scheduled recheck must not restart the original local-retention clock.
    await workflow.pool.execute("UPDATE remote_assets SET reverified_at=now()-interval '31 days'")
    opened = await workflow.storage.reverify_due(interval_days=30)
    assert len(opened) == 1
    reverify = await workflow.next_activity()
    assert reverify.stage == "verify"
    await submit(workflow, report(reverify, evidence=cold_receipt()))
    await workflow.consume_results()
    assert await workflow.next_activity() is None
    assert await workflow.pool.fetchval("SELECT local_cleanup_after FROM videos") == expected


async def test_readback_retry_clears_current_failure_and_retains_attempt_history_bdd_032_054(workflow):
    await reach_upload(workflow)
    await add_account(workflow)
    upload = await workflow.next_activity()
    await submit(workflow, report(upload, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()
    first = await workflow.next_activity()
    await submit(workflow, report(first, "infrastructure", error_code="DEPENDENCY_FAILURE"))
    await workflow.consume_results()
    await workflow.retry_now(first.execution_id, operator="fixture-operator", reason="provider is available")

    retry = await workflow.next_activity()
    assert retry.execution_id == first.execution_id and retry.operation_id == first.operation_id
    await submit(workflow, report(retry, evidence=cold_receipt()))
    await workflow.consume_results()
    assert await workflow.next_activity() is None

    row = await workflow.pool.fetchrow("SELECT state,error_code,failure_class,blocked_reason FROM executions")
    assert dict(row) == {"state": "SUCCEEDED", "error_code": None, "failure_class": None, "blocked_reason": None}
    assert await workflow.pool.fetchval("SELECT state FROM remote_assets") == "DURABLE"
    assert await workflow.pool.fetchval("SELECT count(*) FROM activity_attempts WHERE stage='upload'") == 1
    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts") == SIZE
    assert (
        await workflow.pool.fetchval(
            "SELECT payload->>'error_code' FROM activity_results WHERE attempt_id=$1", first.attempt_id
        )
        == "DEPENDENCY_FAILURE"
    )


async def test_readback_without_independent_provenance_blocks_durability_bdd_052(workflow):
    await reach_upload(workflow)
    await add_account(workflow)
    upload = await workflow.next_activity()
    await submit(workflow, report(upload, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()
    verify = await workflow.next_activity()
    # A receipt that cannot show the bytes came from the provider proves nothing.
    receipt = cold_receipt()
    receipt.pop("cold_inputs")
    await submit(workflow, report(verify, evidence=receipt))
    await workflow.consume_results()
    await workflow.next_activity()

    assert await workflow.pool.fetchval("SELECT state FROM remote_assets") != "DURABLE"


async def test_durability_survives_a_temporary_url_expiry_bdd_002_057(workflow):
    await reach_upload(workflow)
    await add_account(workflow)
    upload = await workflow.next_activity()
    await submit(workflow, report(upload, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()
    verify = await workflow.next_activity()
    receipt = cold_receipt()
    await submit(workflow, report(verify, evidence=receipt))
    await workflow.consume_results()
    await workflow.next_activity()

    # An ephemeral playback URL going stale is not evidence of remote loss.
    await workflow.pool.execute("UPDATE videos SET share_url=NULL,updated_at=now()")
    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets")
    assert asset["state"] == "DURABLE" and asset["invalidated_reason"] is None


async def test_legacy_uploader_cannot_claim_a_managed_video_bdd_019(workflow):
    from pixav.shared.repository import TaskRepository

    video, _ = await reach_upload(workflow)
    assert await TaskRepository(workflow.pool).is_managed_video(video) is True


async def expire_leases(workflow):
    """Age out both halves of an activity lease, as a lost heartbeat would."""
    await workflow.pool.execute("UPDATE executions SET lease_until=now()-interval '1 second'")
    await workflow.pool.execute("UPDATE account_leases SET lease_until=now()-interval '1 second'")


async def reach_durable(workflow):
    """Drive one video all the way to a verified, durable remote asset."""
    video, task = await reach_upload(workflow)
    await add_account(workflow)
    upload = await workflow.next_activity()
    await submit(workflow, report(upload, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()
    verify = await workflow.next_activity()
    receipt = cold_receipt()
    await submit(workflow, report(verify, evidence=receipt))
    await workflow.consume_results()
    # Promotion is the authority's next decision, not a side effect of the report.
    assert await workflow.next_activity() is None
    return video, task


async def test_unknown_effect_reissues_the_same_operation_identity_bdd_048_113(workflow):
    """An upload whose outcome is unknown is reconciled, never blindly resent."""
    await reach_upload(workflow)
    await add_account(workflow)
    upload = await workflow.next_activity()
    # The worker recorded its intent, then lost the ability to say what happened.
    await workflow.pool.execute(
        """UPDATE remote_asset_segments SET state='upload_intent',
        recovery='{"push_intent": true, "media_id": "42"}'::jsonb""",
    )
    await submit(workflow, report(upload, outcome="unknown_effect", error_code="REMOTE_EFFECT_UNKNOWN"))
    await workflow.consume_results()

    assert await workflow.pool.fetchval("SELECT state FROM executions") == "USER_ACTION_REQUIRED"
    segment = await workflow.pool.fetchrow("SELECT * FROM remote_asset_segments")
    assert segment["usage_counted_at"] is None, "an unproven upload is not charged"
    assert segment["recovery"] != "{}", "the guest-side facts survive for reconciliation"
    assert await workflow.pool.fetchval("SELECT count(*) FROM operation_intents WHERE stage='upload'") == 1


async def test_lease_recovery_keeps_one_upload_operation_identity_bdd_048(workflow):
    """Reissuing after a lost lease must address the same external operation."""
    await reach_upload(workflow)
    await add_account(workflow)
    first = await workflow.next_activity()
    await expire_leases(workflow)
    await workflow.recover_expired()
    second = await workflow.next_activity()

    assert second is not None and second.operation_id == first.operation_id
    assert second.generation > first.generation
    identities = await workflow.pool.fetch("SELECT identity FROM operation_intents WHERE stage='upload'")
    assert len(identities) == 1


async def test_a_stale_generation_cannot_advance_an_execution_bdd_001(workflow):
    """Derived state that contradicts PostgreSQL loses; it never advances anything."""
    await reach_upload(workflow)
    await add_account(workflow)
    stale = await workflow.next_activity()
    await expire_leases(workflow)
    await workflow.recover_expired()
    await workflow.next_activity()

    await submit(workflow, report(stale, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()

    segment = await workflow.pool.fetchrow("SELECT * FROM remote_asset_segments")
    assert segment["usage_counted_at"] is None and segment["share_url"] is None
    assert await workflow.pool.fetchval("SELECT state FROM remote_assets") == "REQUESTED"


async def test_domain_facts_outlive_the_derived_queue_bdd_116_001(workflow, integration_redis):
    """Losing Redis loses dispatch, not a durable remote asset or an execution."""
    from pixav.shared.queue import TaskQueue

    client, key = integration_redis
    await reach_upload(workflow)
    await add_account(workflow)
    queue = TaskQueue(redis=client, queue_name=key)
    assert await workflow.tick(queue, storage_queue=queue) == 1

    # Every derived record of that dispatch disappears.
    await client.delete(key, f"{key}:processing")
    await expire_leases(workflow)
    await workflow.recover_expired()

    assert await workflow.tick(queue, storage_queue=queue) == 1
    assert await queue.total_depth() == 1
    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets")
    assert asset is not None and asset["state"] == "REQUESTED"


async def test_durable_assets_survive_the_loss_of_the_queue_bdd_116(workflow, integration_redis):
    from pixav.shared.queue import TaskQueue

    client, key = integration_redis
    await reach_durable(workflow)
    await client.delete(key, f"{key}:processing")

    assert await workflow.pool.fetchval("SELECT state FROM remote_assets") == "DURABLE"
    assert await workflow.tick(TaskQueue(redis=client, queue_name=key)) == 0


async def test_waiting_states_are_reported_apart_from_failures_bdd_124_129(workflow):
    await reach_upload(workflow)
    # No account at all, so the authority has to wait rather than fail.
    assert await workflow.next_activity() is None

    counts = await workflow.observe_states()
    assert counts["waiting_quota"] == 1
    assert counts["terminal"] == {}

    await workflow.pool.execute("UPDATE executions SET state='FAILED',failure_class='infrastructure'")
    counts = await workflow.observe_states()
    assert counts["waiting_quota"] == 0
    assert counts["terminal"] == {"infrastructure": 1}


async def test_source_exhaustion_is_reported_as_a_wait_bdd_123_017(workflow):
    video = await workflow.pool.fetchval("INSERT INTO videos(title) VALUES('no source') RETURNING id")
    await workflow.admit(video, max_retries=1)

    assert await workflow.next_activity() is None

    counts = await workflow.observe_states()
    assert counts["source_unavailable"] == 1
    assert counts["terminal"] == {}, "waiting for a source is not a failure"


async def test_verification_failure_is_distinguishable_from_upload_failure_bdd_125(workflow):
    from pixav.shared import metrics

    def total(counter, reason):
        return counter.labels(reason=reason)._value.get()

    await reach_upload(workflow)
    await add_account(workflow)
    upload = await workflow.next_activity()
    await submit(workflow, report(upload, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()
    verify = await workflow.next_activity()
    before = total(metrics.remote_upload_failures, "READBACK_FAILED")

    await submit(workflow, report(verify, outcome="verification_failed", error_code="READBACK_FAILED"))
    await workflow.consume_results()

    assert total(metrics.remote_verification_failures, "READBACK_FAILED") >= 1
    assert total(metrics.remote_upload_failures, "READBACK_FAILED") == before


# ── photos-original-v2: the manifest as a whole, and asking again later ──────


async def reach_upload_split(workflow):
    """Drive one video to upload with a manifest of two joined segments."""
    video = await workflow.pool.fetchval("INSERT INTO videos(title) VALUES('split fixture') RETURNING id")
    candidates, _ = SourcePolicy().normalize_batch(
        [
            {
                "provider": "synthetic",
                "provider_id": "split",
                "title": "synthetic 1080p .mp4",
                "magnet_uri": "magnet:?xt=urn:btih:" + "b" * 40,
            }
        ]
    )
    await workflow.observe(video, candidates)
    await workflow.admit(video, max_retries=1)
    download = await workflow.next_activity()
    await submit(workflow, report(download, artifact_path="/staging/split.mkv"))
    await workflow.consume_results()
    prepare = await workflow.next_activity()
    facts = MediaFacts.model_validate(FACTS)
    artifact = PreparedArtifact(
        path="/staging/split.mp4",
        input_path=prepare.input_path,
        input_sha256="b" * 64,
        facts=facts,
        input_facts=facts.model_copy(update={"container": "matroska", "sha256": "b" * 64}),
        segments=(
            {
                "index": 0,
                "path": "/staging/split-0.mp4",
                "size_bytes": 1024,
                "sha256": "d" * 64,
                "start_seconds": 0.0,
                "end_seconds": 4.0,
            },
            {
                "index": 1,
                "path": "/staging/split-1.mp4",
                "size_bytes": 1024,
                "sha256": "e" * 64,
                "start_seconds": 4.0,
                "end_seconds": 10.0,
            },
        ),
    )
    await submit(workflow, report(prepare, prepared=artifact))
    await workflow.consume_results()
    return video


def part_receipt(digest: str, span: float):
    """What a cold read-back of one split part reports about itself."""
    observed = {**FACTS, "size_bytes": 1024, "sha256": digest, "duration_seconds": span}
    return cold_receipt(size=1024, sha256=digest, observed=observed)


async def upload_and_verify_split(workflow):
    """Carry both segments through creation and cold read-back."""
    await add_account(workflow, daily_quota_bytes=10_000_000)
    for digest in ("d" * 64, "e" * 64):
        upload = await workflow.next_activity()
        assert upload.stage == "upload"
        await submit(workflow, report(upload, share_url=f"{SHARE}-{digest[:4]}", evidence=BACKUP))
        await workflow.consume_results()
    for digest, span in (("d" * 64, 4.0), ("e" * 64, 6.0)):
        verify = await workflow.next_activity()
        assert verify.stage == "verify"
        await submit(workflow, report(verify, evidence=part_receipt(digest, span)))
        await workflow.consume_results()


async def test_verification_is_committed_before_durability_bdd_053_056(workflow):
    """VERIFIED is a state the pipeline actually reaches, not a declared one."""
    await reach_upload(workflow)
    await add_account(workflow)
    upload = await workflow.next_activity()
    await submit(workflow, report(upload, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()
    assert await workflow.pool.fetchval("SELECT state FROM remote_assets") == "CREATED"

    verify = await workflow.next_activity()
    await submit(workflow, report(verify, evidence=cold_receipt()))
    await workflow.consume_results()

    # Every segment has come back cold and whole, and nothing is durable yet:
    # the manifest as a whole has not answered for itself.
    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets")
    assert asset["state"] == "VERIFIED" and asset["durable_at"] is None

    assert await workflow.next_activity() is None
    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets")
    assert asset["state"] == "DURABLE" and asset["durable_at"] is not None
    assert asset["reverified_at"] is not None


async def test_a_readback_of_different_media_never_becomes_durable_bdd_053(workflow):
    """Matching bytes with the wrong codecs is not the prepared artifact."""
    await reach_upload(workflow)
    await add_account(workflow)
    upload = await workflow.next_activity()
    await submit(workflow, report(upload, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()
    verify = await workflow.next_activity()
    observed = {
        **FACTS,
        "streams": [
            {"kind": "video", "codec": "vp9", "width": 1920, "height": 1080},
            {"kind": "audio", "codec": "aac", "width": 0, "height": 0},
        ],
    }

    await submit(workflow, report(verify, evidence=cold_receipt(observed=observed)))
    await workflow.consume_results()
    await workflow.next_activity()

    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets")
    assert asset["state"] == "CREATED" and asset["durable_at"] is None
    assert await workflow.pool.fetchval("SELECT state FROM executions") != "SUCCEEDED"
    assert await workflow.pool.fetchval("SELECT local_path FROM videos") == "/staging/prepared.mp4"


async def test_a_readback_without_observed_media_never_becomes_durable_bdd_053(workflow):
    """A v2 asset cannot be promoted on v1 evidence a stale image would produce."""
    await reach_upload(workflow)
    await add_account(workflow)
    upload = await workflow.next_activity()
    await submit(workflow, report(upload, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()
    verify = await workflow.next_activity()
    receipt = cold_receipt()
    receipt.pop("observed")

    await submit(workflow, report(verify, evidence=receipt))
    await workflow.consume_results()
    await workflow.next_activity()

    assert await workflow.pool.fetchval("SELECT state FROM remote_assets") == "CREATED"


async def test_a_joined_manifest_becomes_durable_bdd_056(workflow):
    await reach_upload_split(workflow)
    assert await workflow.pool.fetchval("SELECT segment_count FROM remote_assets") == 2

    await upload_and_verify_split(workflow)
    assert await workflow.pool.fetchval("SELECT state FROM remote_assets") == "VERIFIED"
    assert await workflow.next_activity() is None

    assert await workflow.pool.fetchval("SELECT state FROM remote_assets") == "DURABLE"


async def test_a_manifest_that_stopped_covering_the_film_is_refused_bdd_056(workflow):
    """Every segment can be individually correct and still not be the film.

    The junction rule is applied again to what was actually persisted, so drift
    between the accepted plan and the stored manifest cannot pass as durable.
    """
    await reach_upload_split(workflow)
    await upload_and_verify_split(workflow)
    await workflow.pool.execute("UPDATE remote_asset_segments SET start_seconds=6.0 WHERE segment_index=1")

    assert await workflow.next_activity() is None

    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets")
    assert asset["state"] == "VERIFIED" and asset["durable_at"] is None
    execution = await workflow.pool.fetchrow("SELECT * FROM executions ORDER BY created_at DESC LIMIT 1")
    assert execution["state"] == "FAILED" and execution["blocked_reason"] == "REMOTE_MANIFEST_INCONSISTENT"


async def age_out_durability(workflow, days: int = 2):
    """Put the durable asset's last independent look that many days in the past."""
    await workflow.pool.execute(
        "UPDATE remote_assets SET durable_at=now()-make_interval(days=>$1),reverified_at=NULL", days
    )


async def test_durable_assets_are_asked_about_again_bdd_055(workflow):
    """A copy nobody ever looks at again is an assumption, not a fact."""
    await reach_durable(workflow)
    await age_out_durability(workflow)

    opened = await workflow.storage.reverify_due(interval_days=1)

    assert len(opened) == 1
    execution = await workflow.pool.fetchrow("SELECT * FROM executions WHERE id=$1", opened[0])
    assert execution["stage"] == "verify" and execution["state"] == "READY"
    assert json.loads(execution["checkpoint"])["reverification"] is True
    # Creation stands; it is the verification that is being asked again.
    assert await workflow.pool.fetchval("SELECT count(*) FROM remote_asset_segments WHERE state='backed_up'") == 1
    assert await workflow.pool.fetchval("SELECT state FROM remote_assets") == "DURABLE"


async def test_re_verification_never_uploads_or_spends_quota_bdd_055(workflow):
    await reach_durable(workflow)
    await age_out_durability(workflow)
    spent = await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts")
    await workflow.storage.reverify_due(interval_days=1)

    request = await workflow.next_activity()

    assert request is not None and request.stage == "verify"
    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts") == spent
    assert await workflow.pool.fetchval("SELECT count(*) FROM executions WHERE stage='upload'") == 0


async def test_a_successful_re_verification_refreshes_the_evidence_bdd_055(workflow):
    await reach_durable(workflow)
    await age_out_durability(workflow)
    durable_at = await workflow.pool.fetchval("SELECT durable_at FROM remote_assets")
    await workflow.storage.reverify_due(interval_days=1)

    verify = await workflow.next_activity()
    await submit(workflow, report(verify, evidence=cold_receipt()))
    await workflow.consume_results()
    assert await workflow.next_activity() is None

    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets")
    assert asset["state"] == "DURABLE"
    # The transition is not repeated, so the original durability timestamp
    # stands; only the record of when it was last looked at moves.
    assert asset["durable_at"] == durable_at
    assert asset["reverified_at"] > durable_at
    assert "reverification" in json.loads(asset["evidence"])


async def test_a_failed_re_verification_withdraws_durability_bdd_055_125(workflow):
    """Read back once and unreadable now is confirmed loss, not a pending upload."""
    from pixav.shared import metrics

    await reach_durable(workflow)
    await age_out_durability(workflow)
    before = metrics.remote_assets_invalidated.labels(reason="READBACK_FAILED")._value.get()
    await workflow.storage.reverify_due(interval_days=1)

    verify = await workflow.next_activity()
    await submit(workflow, report(verify, "verification_failed", error_code="READBACK_FAILED"))
    await workflow.consume_results()

    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets")
    assert asset["state"] == "INVALID" and asset["durable_at"] is None
    assert asset["invalidated_reason"] == "READBACK_FAILED"
    assert metrics.remote_assets_invalidated.labels(reason="READBACK_FAILED")._value.get() == before + 1
    # The local copy is the only one left and is never cleaned up now.
    assert await workflow.pool.fetchval("SELECT local_path FROM videos") == "/staging/prepared.mp4"


async def test_a_failure_before_durability_is_not_remote_loss_bdd_055(workflow):
    """Not yet proven and no longer true are different claims about the provider."""
    await reach_upload(workflow)
    await add_account(workflow)
    upload = await workflow.next_activity()
    await submit(workflow, report(upload, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()
    verify = await workflow.next_activity()

    await submit(workflow, report(verify, "verification_failed", error_code="READBACK_FAILED"))
    await workflow.consume_results()

    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets")
    assert asset["state"] == "CREATED" and asset["invalidated_reason"] is None


async def test_an_operator_may_withdraw_a_copy_they_confirmed_is_gone_bdd_055(workflow):
    await reach_durable(workflow)
    asset_id = await workflow.pool.fetchval("SELECT id FROM remote_assets")
    storage = StorageWorkflow(workflow.pool)

    with pytest.raises(ValueError, match="operator and reason"):
        await storage.invalidate_asset(asset_id, operator=" ", reason="album deleted")

    previous = await storage.invalidate_asset(asset_id, operator="fixture-operator", reason="album deleted")

    assert previous == "DURABLE"
    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets")
    assert asset["state"] == "INVALID" and asset["durable_at"] is None
    assert "album deleted" in asset["invalidated_reason"]


async def test_the_reverification_backlog_is_observable_bdd_055(workflow):
    await reach_durable(workflow)

    assert await workflow.storage.due_reverification(interval_days=1) == 0

    await age_out_durability(workflow)

    assert await workflow.storage.due_reverification(interval_days=1) == 1
    assert await workflow.storage.due_reverification(interval_days=0) == 0, "0 disables re-verification"
    assert await workflow.storage.reverify_due(interval_days=0) == []


@pytest.fixture
async def reuse_pair(workflow):
    import hashlib

    await reach_upload(workflow)
    account = await add_account(workflow)
    source = await workflow.next_activity()
    await submit(workflow, report(source, share_url=SHARE, evidence=BACKUP))
    await workflow.consume_results()
    verify = await workflow.next_activity()
    await submit(workflow, report(verify, evidence=cold_receipt()))
    await workflow.consume_results()
    assert await workflow.next_activity() is None
    await reach_upload(workflow, info_hash="b" * 40)
    target = await workflow.next_activity()
    await submit(workflow, report(target, outcome="user_action", error_code="USER_ACTION_REQUIRED"))
    await workflow.consume_results()
    at = await workflow.pool.fetchval("SELECT clock_timestamp()")
    evidence = cold_receipt(
        at=at.isoformat(),
        cold_mounts=["/app/src:ro", "/work:rw"],
        provider_asset_id=str(source.asset_id),
        provider_segment_index=0,
        provider_share_sha256=hashlib.sha256(SHARE.encode()).hexdigest(),
    )
    return source, target, account, evidence


async def test_reconcile_existing_dry_run_idempotent_no_debit_then_fresh_verify(workflow, reuse_pair):
    from pixav.shared.remote_assets import segment_from_row

    source, target, account, evidence = reuse_pair
    storage = StorageWorkflow(workflow.pool)
    args = (target.execution_id, 0, source.asset_id, 0, evidence)
    audit = {"operator": "fixture", "reason": "same original independently recovered"}
    before = await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account)
    assert await storage.reconcile_existing_segment(*args, **audit)
    assert (
        await workflow.pool.fetchval("SELECT state FROM executions WHERE id=$1", target.execution_id)
        == "USER_ACTION_REQUIRED"
    )
    assert await storage.reconcile_existing_segment(*args, **audit, apply=True)
    assert not await storage.reconcile_existing_segment(*args, **audit, apply=True)
    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == before
    row = await workflow.pool.fetchrow("SELECT * FROM remote_asset_segments WHERE asset_id=$1", target.asset_id)
    segment = segment_from_row(row)
    assert segment.provider_filename != segment.filename
    assert str(source.asset_id) in segment.provider_filename
    request = await workflow.next_activity()
    assert request.stage == "verify" and request.execution_id == target.execution_id
    assert await workflow.pool.fetchval("SELECT state FROM remote_assets WHERE id=$1", target.asset_id) == "CREATED"
    await submit(workflow, report(request, evidence=cold_receipt()))
    await workflow.consume_results()
    assert await workflow.next_activity() is None
    assert await workflow.pool.fetchval("SELECT state FROM remote_assets WHERE id=$1", target.asset_id) == "DURABLE"
    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == before
    assert (
        await workflow.pool.fetchval(
            "SELECT count(*) FROM activity_attempts WHERE execution_id=$1 AND stage='upload'", target.execution_id
        )
        == 1
    )


@pytest.mark.parametrize("defect", ["hash", "account", "source", "staging", "stale", "duration", "active"])
async def test_reconcile_existing_refuses_unproven_or_active_target(workflow, reuse_pair, defect):
    from datetime import timedelta

    source, target, account, evidence = reuse_pair
    if defect == "hash":
        evidence["sha256"] = "d" * 64
    elif defect == "account":
        other = await add_account(workflow)
        await workflow.pool.execute(
            "UPDATE remote_asset_segments SET account_id=$2 WHERE asset_id=$1", target.asset_id, other
        )
    elif defect == "source":
        evidence["provider_asset_id"] = str(uuid.uuid4())
    elif defect == "staging":
        evidence["cold_mounts"].append("/parts:ro")
    elif defect == "stale":
        now = await workflow.pool.fetchval("SELECT clock_timestamp()")
        evidence["at"] = (now - timedelta(hours=2)).isoformat()
    elif defect == "duration":
        evidence["observed"]["duration_seconds"] = 2
    elif defect == "active":
        await workflow.pool.execute("UPDATE executions SET state='READY' WHERE id=$1", target.execution_id)
    before = await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account)
    with pytest.raises(ValueError):
        await StorageWorkflow(workflow.pool).reconcile_existing_segment(
            target.execution_id,
            0,
            source.asset_id,
            0,
            evidence,
            operator="fixture",
            reason="reviewed",
            apply=True,
        )
    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == before
    assert (
        await workflow.pool.fetchval(
            "SELECT usage_counted_at FROM remote_asset_segments WHERE asset_id=$1", target.asset_id
        )
        is None
    )


async def test_failed_retrieval_can_only_resume_verification_with_new_proof(workflow, reuse_pair):
    source, target, account, evidence = reuse_pair
    storage = StorageWorkflow(workflow.pool)
    await storage.reconcile_existing_segment(
        target.execution_id, 0, source.asset_id, 0, evidence, operator="fixture", reason="reviewed", apply=True
    )

    async def next_due_verification():
        # Reconciliation schedules by the DB clock. A host clock correction
        # can make its just-written due_at briefly future; do not edit that fact.
        async def wait_for_due():
            while True:
                request = await workflow.next_activity()
                if request is not None:
                    return request
                timing = await workflow.pool.fetchrow(
                    "SELECT state,due_at,clock_timestamp() AS checked_at FROM executions WHERE id=$1",
                    target.execution_id,
                )
                assert timing["state"] == "READY" and timing["due_at"] > timing["checked_at"], dict(timing)
                await asyncio.sleep(min(1, (timing["due_at"] - timing["checked_at"]).total_seconds() + 0.01))

        return await asyncio.wait_for(wait_for_due(), timeout=10)

    verify = await next_due_verification()
    await submit(workflow, report(verify, outcome="verification_failed", error_code="READBACK_FAILED"))
    await workflow.consume_results()
    proof = {**evidence, "provider_asset_id": str(target.asset_id)}
    with pytest.raises(ValueError):
        await storage.retry_verification(
            target.execution_id, 0, {**proof, "sha256": "f" * 64}, operator="fixture", reason="fresh proof", apply=True
        )
    assert await storage.retry_verification(target.execution_id, 0, proof, operator="fixture", reason="fresh proof")
    assert await workflow.pool.fetchval("SELECT state FROM executions WHERE id=$1", target.execution_id) == "FAILED"
    assert await storage.retry_verification(
        target.execution_id, 0, proof, operator="fixture", reason="fresh proof", apply=True
    )
    retried = await next_due_verification()
    assert retried.stage == "verify" and retried.operation_id == verify.operation_id
    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == SIZE
    assert (
        await workflow.pool.fetchval(
            "SELECT count(*) FROM activity_attempts WHERE execution_id=$1 AND stage='upload'", target.execution_id
        )
        == 1
    )
    checkpoint = json.loads(
        await workflow.pool.fetchval("SELECT checkpoint FROM executions WHERE id=$1", target.execution_id)
    )
    assert checkpoint["verification_recoveries"][0]["previous_error_code"] == "READBACK_FAILED"


@pytest.mark.parametrize("defect", [None, "filename", "account", "generation", "unconfirmed"])
async def test_share_only_review_is_queued_without_claiming_success(workflow, held_upload_review, defect):
    upload, account, evidence, recovery = held_upload_review
    from pixav.shared.remote_assets import segment_from_row

    segment = segment_from_row(
        await workflow.pool.fetchrow("SELECT * FROM remote_asset_segments WHERE asset_id=$1", upload.asset_id)
    )
    for key in ("share_url", "backup_evidence", "cold_readback"):
        evidence.pop(key)
    evidence.update(
        version="upload-outcome-review-v2",
        outcome="EXISTING_ITEM_REQUIRES_COLD",
        filename=segment.filename,
        same_account_confirmed=True,
        backed_up=True,
        original_quality=True,
    )
    if defect == "filename":
        evidence["filename"] = "pixav-wrong.mp4"
    elif defect == "account":
        evidence["account_id"] = str(uuid.uuid4())
    elif defect == "generation":
        evidence["generation"] += 1
    elif defect == "unconfirmed":
        evidence["same_account_confirmed"] = False
    audit = {"operator": "fixture", "reason": "existing exact candidate without share location"}
    before = await workflow.pool.fetchval("SELECT checkpoint FROM executions WHERE id=$1", upload.execution_id)
    if defect:
        with pytest.raises(ValueError):
            await workflow.storage.review_upload_outcome(upload.execution_id, 0, evidence, **audit, apply=True)
        assert (
            await workflow.pool.fetchval("SELECT checkpoint FROM executions WHERE id=$1", upload.execution_id) == before
        )
        return
    assert await workflow.storage.review_upload_outcome(upload.execution_id, 0, evidence, **audit)
    assert await workflow.pool.fetchval("SELECT checkpoint FROM executions WHERE id=$1", upload.execution_id) == before
    assert await workflow.storage.review_upload_outcome(upload.execution_id, 0, evidence, **audit, apply=True)
    assert not await workflow.storage.review_upload_outcome(upload.execution_id, 0, evidence, **audit, apply=True)
    assert await workflow.storage.consume_upload_reviews() == 1
    next_request = await workflow.next_activity()
    assert next_request.operation_id == upload.operation_id and next_request.account_id == account
    saved = json.loads(
        await workflow.pool.fetchval("SELECT recovery FROM remote_asset_segments WHERE asset_id=$1", upload.asset_id)
    )
    assert saved["upload_outcome_review"]["recovery_before"] == recovery
    assert all(saved[k] == v for k, v in recovery.items())
    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == 0
    assert (
        await workflow.pool.fetchval("SELECT count(*) FROM remote_asset_segments WHERE usage_counted_at IS NOT NULL")
        == 0
    )
    result = report(next_request, share_url=SHARE, evidence=BACKUP)
    assert await submit(workflow, result)
    assert await workflow.consume_results() == 1
    assert not await submit(workflow, result)
    assert await workflow.consume_results() == 0
    assert await workflow.pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == SIZE
    assert await workflow.pool.fetchval("SELECT count(*) FROM operation_intents WHERE stage='upload'") == 1
