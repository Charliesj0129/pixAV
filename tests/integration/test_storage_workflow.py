"""Real PostgreSQL account leasing, exactly-once quota and durability commits.

Every assertion here is about pixAV's own bookkeeping. The provider is
represented by reported activity results; nothing here demonstrates that a real
Google Photos upload or cold read-back succeeded.
"""

import json
import uuid
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


async def reach_upload(workflow):
    """Drive one video from discovery to the point where storage takes over."""
    video = await workflow.pool.fetchval("INSERT INTO videos(title) VALUES('synthetic fixture') RETURNING id")
    candidates, _ = SourcePolicy().normalize_batch(
        [
            {
                "provider": "synthetic",
                "provider_id": "one",
                "title": "synthetic 1080p .mp4",
                "magnet_uri": "magnet:?xt=urn:btih:" + "a" * 40,
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
