"""Real PostgreSQL execution ownership, attempts, clock and storage handoff."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest

from pixav.maxwell_core.media_workflow import MediaWorkflow
from pixav.media_loader.preparation import MediaFacts, PreparedArtifact
from pixav.shared.workflow import ActivityResult
from pixav.sht_probe.policy import SourcePolicy

pytestmark = pytest.mark.integration


@pytest.fixture
async def workflow(integration_db):
    for path in sorted(Path("migrations").glob("*.sql")):
        await integration_db.execute(path.read_text())
    return MediaWorkflow(integration_db, backoff=(1, 2), lease_seconds=120)


async def seed(workflow):
    video = await workflow.pool.fetchval("INSERT INTO videos(title) VALUES('synthetic fixture') RETURNING id")
    policy = SourcePolicy()
    candidates, _ = policy.normalize_batch(
        [
            {
                "provider": "synthetic",
                "provider_id": "one",
                "title": "synthetic 1080p .mp4",
                "magnet_uri": "magnet:?xt=urn:btih:" + "a" * 40,
            },
            {
                "provider": "other",
                "provider_id": "two",
                "title": "synthetic 720p .mp4",
                "magnet_uri": "magnet:?xt=urn:btih:" + "b" * 40,
            },
        ]
    )
    await workflow.observe(video, candidates)
    task = await workflow.admit(video, max_retries=1)
    return video, task


def report(request, outcome="success", **kwargs):
    return ActivityResult(**request.model_dump(exclude={"stage", "identity", "input_path"}), outcome=outcome, **kwargs)


async def submit(workflow, result):
    return await workflow.pool.fetchval("SELECT report_activity($1::jsonb)", result.model_dump_json())


async def test_unique_admission_and_source_wait_bdd_017_018_021(workflow):
    video = await workflow.pool.fetchval("INSERT INTO videos(title) VALUES('synthetic') RETURNING id")
    tasks = await asyncio.gather(workflow.admit(video), workflow.admit(video))
    assert tasks[0] == tasks[1]
    assert await workflow.next_activity() is None
    assert await workflow.pool.fetchval("SELECT count(*) FROM activity_attempts") == 0
    assert await workflow.pool.fetchval("SELECT blocked_reason FROM executions") == "SOURCE_UNAVAILABLE"
    candidates, _ = SourcePolicy().normalize_batch(
        [
            dict(
                provider="synthetic",
                provider_id="new",
                title="1080p .mp4",
                magnet_uri="magnet:?xt=urn:btih:" + "a" * 40,
            )
        ]
    )
    await workflow.observe(video, candidates)
    assert (await workflow.next_activity()).task_id == tasks[0]


async def test_handoff_is_atomic_and_duplicate_report_is_inert_bdd_022_023_035(workflow):
    _, task = await seed(workflow)
    request = await workflow.next_activity()
    assert request.task_id == task
    assert await submit(workflow, report(request, artifact_path="/staging/synthetic.mkv"))
    assert not await submit(workflow, report(request, artifact_path="/staging/duplicate.mkv"))
    assert await workflow.consume_results() == 1
    assert await workflow.consume_results() == 0
    prepare = await workflow.next_activity()
    assert prepare.stage == "prepare"
    facts = MediaFacts(
        container="mov,mp4",
        size_bytes=100,
        duration_seconds=10,
        sha256="a" * 64,
        streams=({"kind": "video", "codec": "h264", "width": 1920, "height": 1080}, {"kind": "audio", "codec": "aac"}),
    )
    artifact = PreparedArtifact(
        path="/staging/prepared.mp4",
        input_path=prepare.input_path,
        input_sha256="b" * 64,
        facts=facts,
        input_facts=facts.model_copy(update={"container": "matroska", "sha256": "b" * 64}),
    )
    result = report(prepare, prepared=artifact)
    assert await submit(workflow, result)
    await workflow.consume_results()
    assert not await submit(workflow, result)
    await workflow.consume_results()
    # The handoff is a stage change inside the same execution. Creating a second
    # task here would put a legacy uploader on the same work (BDD-019).
    assert await workflow.pool.fetchval("SELECT count(*) FROM tasks WHERE queue_name='pixav:upload'") == 0
    execution = await workflow.pool.fetchrow("SELECT * FROM executions")
    assert execution["stage"] == "upload" and execution["state"] == "READY"
    asset = await workflow.pool.fetchrow("SELECT * FROM remote_assets")
    assert asset["state"] == "REQUESTED" and asset["provider"] == "google_photos"
    assert asset["video_id"] == await workflow.pool.fetchval(
        "SELECT video_id FROM workflow_tasks WHERE task_id=$1", task
    )
    assert await workflow.pool.fetchval("SELECT count(*) FROM activity_attempts") == 2


async def test_retry_clock_fencing_and_manual_replay_bdd_020_021_031_033(workflow):
    _, task = await seed(workflow)
    request = await workflow.next_activity()
    await submit(workflow, report(request, "infrastructure", error_code="NETWORK"))
    await workflow.consume_results()
    row = await workflow.pool.fetchrow("SELECT *, due_at > now() AS future FROM executions")
    assert row["state"] == "WAITING_RETRY" and row["future"]
    assert await workflow.next_activity() is None
    await workflow.pool.execute("UPDATE executions SET due_at=now()-interval '1 second'")
    next_request = await workflow.next_activity()
    assert next_request.attempt_id != request.attempt_id
    assert next_request.task_id == task
    assert not await submit(workflow, report(request, artifact_path="/stale"))
    await submit(workflow, report(next_request, "infrastructure"))
    await workflow.consume_results()
    assert await workflow.pool.fetchval("SELECT state FROM executions") == "FAILED"
    assert await workflow.next_activity() is None
    new = await workflow.replay(request.execution_id, operator="test-operator", reason="dependency restored")
    assert new != request.execution_id
    assert await workflow.pool.fetchval("SELECT replay_of FROM executions WHERE id=$1", new) == request.execution_id
    assert await workflow.pool.fetchval("SELECT count(*) FROM activity_attempts") == 2


async def test_expired_download_reconciles_same_intent_bdd_024_034(workflow):
    await seed(workflow)
    first = await workflow.next_activity()
    await workflow.pool.execute("UPDATE executions SET lease_until=now()-interval '1 second'")
    assert await workflow.recover_expired() == 1
    second = await workflow.next_activity()
    assert second.operation_id == first.operation_id
    assert second.generation == first.generation + 1
    assert not await submit(workflow, report(first, artifact_path="/stale"))
    assert await submit(workflow, report(second, artifact_path="/reconciled"))


async def test_worker_cannot_mutate_execution_or_managed_task_bdd_019(workflow):
    await seed(workflow)
    request = await workflow.next_activity()
    async with workflow.pool.acquire() as conn:
        await conn.execute("SET ROLE pixav_activity_worker")
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("UPDATE executions SET state='SUCCEEDED'")
            assert await conn.fetchval("SELECT claim_activity($1,$2)", request.attempt_id, uuid4())
            assert not await conn.fetchval("SELECT claim_activity($1,$2)", request.attempt_id, uuid4())
            assert await conn.fetchval(
                "SELECT report_activity($1::jsonb)", report(request, "infrastructure").model_dump_json()
            )
        finally:
            await conn.execute("RESET ROLE")
        # Simulate a legacy role with direct task UPDATE privilege: trigger still refuses.
        await conn.execute("GRANT UPDATE,SELECT ON tasks,videos TO pixav_activity_worker")
        await conn.execute("SET ROLE pixav_activity_worker")
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="managed task"):
                await conn.execute("UPDATE tasks SET state='complete'")
        finally:
            await conn.execute("RESET ROLE")


async def test_source_fallback_preserves_infrastructure_budget_bdd_014_016_032(workflow):
    video, _ = await seed(workflow)
    first = await workflow.next_activity()
    await submit(workflow, report(first, "source_unavailable"))
    await workflow.consume_results()
    second = await workflow.next_activity()
    assert second.identity != first.identity
    assert await workflow.pool.fetchval("SELECT infrastructure_retries FROM executions") == 0
    assert await workflow.pool.fetchval("SELECT count(*) FROM source_observations WHERE video_id=$1", video) == 2
    row = await workflow.pool.fetchrow("SELECT * FROM source_candidates WHERE info_hash=$1", first.identity)
    original_due = row["unavailable_until"]
    observation = json.loads(
        await workflow.pool.fetchval("SELECT observation FROM source_observations WHERE info_hash=$1", first.identity)
    )
    from pixav.sht_probe.policy import CandidateObservation

    await workflow.observe(video, [CandidateObservation.model_validate(observation)])
    assert (
        await workflow.pool.fetchval("SELECT unavailable_until FROM source_candidates WHERE id=$1", row["id"])
        == original_due
    )


async def test_redis_duplicate_ack_loss_and_missing_dispatch(workflow, integration_redis, tmp_path):
    """BDD-020/024/034: Redis redelivery never duplicates a claimed activity."""
    from unittest.mock import AsyncMock

    from pixav.media_loader.activity import MediaActivityWorker
    from pixav.shared.queue import TaskQueue

    redis, key = integration_redis
    queue = TaskQueue(redis, key)
    await seed(workflow)
    request = await workflow.next_activity()
    media = tmp_path / "synthetic.mkv"
    media.write_bytes(b"downloaded artifact")
    client = AsyncMock()
    client.reconcile_download.return_value = str(media)
    worker = MediaActivityWorker(workflow.pool, client, AsyncMock(), output_dir=str(tmp_path))
    await queue.push(request.model_dump(mode="json"))
    await worker.run_one(queue)
    assert await workflow.pool.fetchval("SELECT count(*) FROM activity_results") == 1
    # Simulate ACK loss by redelivering the original immutable envelope.
    await queue.push(request.model_dump(mode="json"))
    await worker.run_one(queue)
    client.reconcile_download.assert_awaited_once()
    assert await workflow.consume_results() == 1
    prepared = await workflow.next_activity()
    await queue.push(prepared.model_dump(mode="json"))
    await redis.delete(key)
    await workflow.pool.execute(
        "UPDATE executions SET lease_until=now()-interval '1 second' WHERE id=$1", prepared.execution_id
    )
    await workflow.recover_expired()
    resumed = await workflow.next_activity()
    assert resumed.operation_id == prepared.operation_id
    assert resumed.attempt_id != prepared.attempt_id


async def test_discovery_observations_do_not_duplicate_tasks_or_infer_title_identity(workflow):
    """BDD-007/009/018: cross-provider observations and independent torrent identities."""
    payload = dict(
        provider="alpha", provider_id="one", title="synthetic 1080p .mp4", magnet_uri="magnet:?xt=urn:btih:" + "a" * 40
    )
    first = await workflow.ingest_observation(payload)
    assert await workflow.ingest_observation({**payload, "provider": "beta"}) == first
    second = await workflow.ingest_observation({**payload, "magnet_uri": "magnet:?xt=urn:btih:" + "b" * 40})
    assert second != first
    assert await workflow.pool.fetchval("SELECT count(*) FROM workflow_tasks") == 2
    assert await workflow.pool.fetchval("SELECT count(*) FROM source_observations") == 3


async def test_configured_policy_and_database_ranking_agree_bdd_010_011_012(workflow):
    workflow.source_policy = SourcePolicy(min_score=90)
    video = await workflow.pool.fetchval("INSERT INTO videos(title) VALUES('synthetic') RETURNING id")
    await workflow.admit(video)
    observations, _ = workflow.source_policy.normalize_batch(
        [
            dict(provider="alpha", provider_id="z", title="1080p .mp4", magnet_uri="magnet:?xt=urn:btih:" + "a" * 40),
            dict(provider="alpha", provider_id="a", title="1080p .mp4", magnet_uri="magnet:?xt=urn:btih:" + "b" * 40),
            dict(provider="beta", provider_id="best", title="4k .iso", magnet_uri="magnet:?xt=urn:btih:" + "c" * 40),
            dict(provider="gamma", provider_id="low", title="720p", magnet_uri="magnet:?xt=urn:btih:" + "d" * 40),
        ]
    )
    await workflow.observe(video, observations)
    now = await workflow.pool.fetchval("SELECT now()")
    expected = workflow.source_policy.select(observations, now=now).selected
    assert (await workflow.next_activity()).identity == expected.info_hash == "b" * 40


async def test_repeat_worker_loss_stops_for_reconciliation_bdd_033(workflow):
    await seed(workflow)
    first = await workflow.next_activity()
    await workflow.pool.execute("UPDATE executions SET lease_until=now()-interval '1 second'")
    await workflow.recover_expired()
    second = await workflow.next_activity()
    assert second.operation_id == first.operation_id
    await workflow.pool.execute("UPDATE executions SET lease_until=now()-interval '1 second'")
    await workflow.recover_expired()
    assert await workflow.next_activity() is None
    status = await workflow.inspect(first.execution_id)
    assert status.state == "USER_ACTION_REQUIRED"
    assert status.recovery_count == 2
    assert len(status.attempts) == 2
    assert status.error_code == "ACTIVITY_LEASE_EXPIRED"


async def test_custom_lease_and_cancellation_fence_worker_bdd_019(workflow):
    await seed(workflow)
    workflow.lease_seconds = 35
    command = await workflow.next_activity()
    token = uuid4()
    assert await workflow.pool.fetchval("SELECT claim_activity($1,$2)", command.attempt_id, token)
    assert await workflow.pool.fetchval("SELECT heartbeat_activity($1,$2)", command.attempt_id, token)
    remaining = await workflow.pool.fetchval("SELECT extract(epoch FROM lease_until-now()) FROM executions")
    assert 30 < remaining <= 35
    assert await workflow.cancel(command.execution_id, operator="test", reason="stop synthetic fixture")
    assert not await workflow.pool.fetchval("SELECT heartbeat_activity($1,$2)", command.attempt_id, token)
    assert not await submit(workflow, report(command, artifact_path="/stale"))
    assert (await workflow.inspect(command.execution_id)).state == "CANCELLED"


async def test_invalid_result_isolated_from_following_results_bdd_026(workflow):
    await seed(workflow)
    command = await workflow.next_activity()
    # SQL accepts only the exact lease, but payload semantics still need validation.
    malformed = {**report(command).model_dump(mode="json"), "outcome": "unexpected"}
    assert await workflow.pool.fetchval("SELECT report_activity($1::jsonb)", json.dumps(malformed))
    await workflow.consume_results()
    assert (await workflow.inspect(command.execution_id)).error_code == "INVALID_ACTIVITY_REPORT"
    video = await workflow.ingest_observation(
        dict(provider="new", provider_id="new", title="1080p .mp4", magnet_uri="magnet:?xt=urn:btih:" + "e" * 40)
    )
    following = await workflow.next_activity()
    assert await submit(workflow, report(following, artifact_path="/staging/synthetic.mp4"))
    assert await workflow.consume_results() == 1
    assert await workflow.pool.fetchval("SELECT count(*) FROM workflow_artifacts") == 1
    assert video is not None


async def test_semantically_invalid_success_does_not_handoff_bdd_026(workflow):
    await seed(workflow)
    command = await workflow.next_activity()
    assert await submit(workflow, report(command))  # no artifact path
    assert await workflow.consume_results() == 1
    assert (await workflow.inspect(command.execution_id)).state == "USER_ACTION_REQUIRED"
    assert await workflow.pool.fetchval("SELECT count(*) FROM workflow_artifacts") == 0
    assert await workflow.pool.fetchval("SELECT count(*) FROM remote_assets") == 0


async def test_authority_role_can_run_complete_media_transitions(workflow):
    """The executable SQL must work under the production role, not just admin."""
    video, _ = await seed(workflow)

    async def set_role(conn):
        await conn.execute("SET ROLE pixav_execution_authority")

    database = await workflow.pool.fetchval("SELECT current_database()")
    endpoint = urlsplit(os.environ["PIXAV_E2E_ADMIN_DSN"])
    pool = await asyncpg.create_pool(
        dsn=urlunsplit(endpoint._replace(path=f"/{database}")), setup=set_role, min_size=1, max_size=2
    )
    try:
        authority = MediaWorkflow(pool)
        command = await authority.next_activity()
        assert await submit(authority, report(command, artifact_path="/staging/synthetic.mkv"))
        assert await authority.consume_results() == 1
        assert (await authority.next_activity()).stage == "prepare"
        assert (await authority.inspect(command.execution_id)).task_id == command.task_id
    finally:
        await pool.close()


async def test_stale_legacy_task_id_cannot_restart_managed_video_bdd_019(workflow):
    video = await workflow.pool.fetchval("INSERT INTO videos(title) VALUES('synthetic legacy') RETURNING id")
    old_task = await workflow.pool.fetchval("INSERT INTO tasks(video_id,state) VALUES($1,'failed') RETURNING id", video)
    await workflow.admit(video)
    async with workflow.pool.acquire() as conn:
        await conn.execute("GRANT SELECT,INSERT,UPDATE ON tasks,videos TO pixav_activity_worker")
        await conn.execute("SET ROLE pixav_activity_worker")
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="managed task"):
                await conn.execute("UPDATE tasks SET state='downloading' WHERE id=$1", old_task)
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="managed task"):
                await conn.execute("INSERT INTO tasks(video_id) VALUES($1)", video)
        finally:
            await conn.execute("RESET ROLE")


async def test_source_exhaustion_is_observable_and_rediscovery_preserves_terminal_state(workflow, caplog):
    import logging

    video, _ = await seed(workflow)
    await workflow.pool.execute(
        "UPDATE source_candidates SET state='unavailable',unavailable_until=now()+interval '1 hour'"
    )
    with caplog.at_level(logging.INFO):
        assert await workflow.next_activity() is None
    assert "source unavailable" in caplog.text
    assert await workflow.pool.fetchval("SELECT count(*) FROM activity_attempts") == 0
    await workflow.pool.execute("UPDATE executions SET state='FAILED'")
    observation = SourcePolicy().normalize(
        dict(provider="new", provider_id="new", title="1080p .mp4", magnet_uri="magnet:?xt=urn:btih:" + "e" * 40)
    )
    await workflow.observe(video, [observation])
    assert await workflow.next_activity() is None
    assert await workflow.pool.fetchval("SELECT state FROM executions") == "FAILED"


async def test_operator_supplied_source_enters_prepare_with_recorded_provenance(workflow, tmp_path):
    """The only legal way in for a file the pipeline did not fetch itself.

    The download stage dispatches a swarm identified by a 40-hex info_hash, so a
    canary file an operator already holds has no route. This one is explicit:
    the artifact is marked operator-supplied with the reference it came from,
    and nothing downstream can read it as a torrent the pipeline downloaded.
    """
    video, task = await seed(workflow)
    execution = await workflow.pool.fetchval("SELECT id FROM executions WHERE task_id=$1", task)
    source = tmp_path / "canary.mp4"
    source.write_bytes(b"canary bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    operation = await workflow.adopt_local_source(
        execution,
        path=str(source),
        declared_sha256=digest,
        source_url="https://example.invalid/canary.mp4",
        operator="tester",
        reason="managed upload canary",
    )

    row = await workflow.pool.fetchrow("SELECT stage,state,checkpoint FROM executions WHERE id=$1", execution)
    checkpoint = json.loads(row["checkpoint"])
    assert (row["stage"], row["state"]) == ("prepare", "READY")
    assert checkpoint == {"download_path": str(source), "download_operation": str(operation)}
    artifact = await workflow.pool.fetchrow(
        "SELECT path,facts FROM workflow_artifacts WHERE operation_id=$1", operation
    )
    facts = json.loads(artifact["facts"])
    assert artifact["path"] == str(source)
    assert facts["provenance"] == "operator-supplied"
    assert facts["source_url"] == "https://example.invalid/canary.mp4"
    assert facts["sha256"] == digest
    assert facts["operator"] == "tester"
    intent = await workflow.pool.fetchrow("SELECT stage,identity FROM operation_intents WHERE id=$1", operation)
    assert intent["stage"] == "download"
    assert intent["identity"] == f"operator-supplied:{digest}"
    assert video is not None


async def test_adoption_refuses_a_file_that_does_not_match_the_declared_hash(workflow, tmp_path):
    """A file that changed between the operator reading it and this call."""
    _, task = await seed(workflow)
    execution = await workflow.pool.fetchval("SELECT id FROM executions WHERE task_id=$1", task)
    source = tmp_path / "canary.mp4"
    source.write_bytes(b"canary bytes")

    with pytest.raises(ValueError, match="declared SHA-256"):
        await workflow.adopt_local_source(
            execution,
            path=str(source),
            declared_sha256="c" * 64,
            source_url="https://example.invalid/canary.mp4",
            operator="tester",
            reason="managed upload canary",
        )
    row = await workflow.pool.fetchrow("SELECT stage,state FROM executions WHERE id=$1", execution)
    assert (row["stage"], row["state"]) == ("download", "READY")


async def test_adoption_refuses_an_execution_that_is_past_the_download_stage(workflow, tmp_path):
    _, task = await seed(workflow)
    execution = await workflow.pool.fetchval("SELECT id FROM executions WHERE task_id=$1", task)
    source = tmp_path / "canary.mp4"
    source.write_bytes(b"canary bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    await workflow.pool.execute("UPDATE executions SET stage='prepare' WHERE id=$1", execution)

    with pytest.raises(ValueError, match="READY download execution"):
        await workflow.adopt_local_source(
            execution,
            path=str(source),
            declared_sha256=digest,
            source_url="https://example.invalid/canary.mp4",
            operator="tester",
            reason="managed upload canary",
        )


async def test_adoption_requires_an_operator_and_a_reason(workflow, tmp_path):
    _, task = await seed(workflow)
    execution = await workflow.pool.fetchval("SELECT id FROM executions WHERE task_id=$1", task)
    source = tmp_path / "canary.mp4"
    source.write_bytes(b"canary bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    with pytest.raises(ValueError, match="operator and reason"):
        await workflow.adopt_local_source(
            execution,
            path=str(source),
            declared_sha256=digest,
            source_url="https://example.invalid/canary.mp4",
            operator="  ",
            reason="managed upload canary",
        )


async def test_an_operator_can_bring_a_waiting_retry_forward(workflow):
    """A backoff outlives its reason once the cause has been repaired."""
    await seed(workflow)
    execution = await workflow.pool.fetchval("SELECT id FROM executions")
    await workflow.pool.execute(
        """UPDATE executions SET state='WAITING_RETRY',infrastructure_retries=3,
        due_at=now()+interval '1 hour' WHERE id=$1""",
        execution,
    )

    await workflow.retry_now(execution, operator="fixture-operator", reason="dependency repaired")

    row = await workflow.pool.fetchrow(
        "SELECT state,infrastructure_retries,due_at<=now() AS due,checkpoint FROM executions WHERE id=$1", execution
    )
    assert row["state"] == "WAITING_RETRY" and row["due"] is True
    # The attempt bound must survive: this moves a clock, it does not forgive.
    assert row["infrastructure_retries"] == 3
    assert json.loads(row["checkpoint"])["retry_now"]["operator"] == "fixture-operator"


async def test_only_a_waiting_execution_may_be_brought_forward(workflow):
    await seed(workflow)
    execution = await workflow.pool.fetchval("SELECT id FROM executions")

    with pytest.raises(ValueError, match="waiting on a retry or quota clock"):
        await workflow.retry_now(execution, operator="fixture-operator", reason="not waiting")
    with pytest.raises(ValueError, match="operator and reason"):
        await workflow.retry_now(execution, operator="", reason="")


async def test_a_diagnosed_infrastructure_failure_is_reopened_on_the_same_execution(workflow):
    """Replay would mint a new artifact and a second upload; reopen keeps the one."""
    await seed(workflow)
    execution = await workflow.pool.fetchval("SELECT id FROM executions")
    await workflow.pool.execute(
        """UPDATE executions SET stage='upload',state='FAILED',failure_class='infrastructure',
        error_code='DEPENDENCY_FAILURE',infrastructure_retries=7,
        checkpoint='{"asset_id":"b8268e94-565a-4997-b040-44932b6b9183"}'::jsonb WHERE id=$1""",
        execution,
    )

    await workflow.reopen(execution, operator="fixture-operator", reason="maestro launch fixed")

    row = await workflow.pool.fetchrow(
        """SELECT id,stage,state,failure_class,error_code,infrastructure_retries,
        due_at<=now() AS due,checkpoint FROM executions WHERE id=$1""",
        execution,
    )
    assert row["id"] == execution and row["stage"] == "upload"
    assert (row["state"], row["due"]) == ("READY", True)
    assert (row["failure_class"], row["error_code"]) == (None, None)
    assert row["infrastructure_retries"] == 0
    checkpoint = json.loads(row["checkpoint"])
    # The asset the segment's recovery journal belongs to must survive untouched.
    assert checkpoint["asset_id"] == "b8268e94-565a-4997-b040-44932b6b9183"
    assert checkpoint["reopen"] == [
        {
            "operator": "fixture-operator",
            "reason": "maestro launch fixed",
            "at": checkpoint["reopen"][0]["at"],
            "retries_forgiven": 7,
        }
    ]
    assert await workflow.pool.fetchval("SELECT count(*) FROM executions") == 1


async def test_only_a_diagnosed_infrastructure_failure_may_be_reopened(workflow):
    """A verification failure is a fact about the remote copy, not a flaky attempt."""
    await seed(workflow)
    execution = await workflow.pool.fetchval("SELECT id FROM executions")

    with pytest.raises(ValueError, match="only a FAILED execution"):
        await workflow.reopen(execution, operator="fixture-operator", reason="not failed")
    with pytest.raises(ValueError, match="operator and reason"):
        await workflow.reopen(execution, operator="", reason="")

    await workflow.pool.execute(
        """UPDATE executions SET state='FAILED',failure_class='verification_failed',
        error_code='READBACK_FAILED' WHERE id=$1""",
        execution,
    )
    with pytest.raises(ValueError, match="only an infrastructure failure"):
        await workflow.reopen(execution, operator="fixture-operator", reason="read-back mismatch")
    assert await workflow.pool.fetchval("SELECT state FROM executions WHERE id=$1", execution) == "FAILED"


async def test_an_infrastructure_failure_never_poisons_the_candidate_bdd_015_032(workflow):
    """A fault on this side is not evidence about the swarm on the other side.

    Cooling a candidate that never had a chance to answer would make a local
    outage look like a dead source, and the pipeline would go on rejecting a
    perfectly good one long after the fault was fixed.
    """
    await seed(workflow)
    request = await workflow.next_activity()
    before = await workflow.pool.fetchrow(
        "SELECT state,unavailable_until,attempts FROM source_candidates WHERE info_hash=$1", request.identity
    )

    await submit(workflow, report(request, "infrastructure", error_code="DEPENDENCY_FAILURE"))
    await workflow.consume_results()

    after = await workflow.pool.fetchrow(
        "SELECT state,unavailable_until,attempts FROM source_candidates WHERE info_hash=$1", request.identity
    )
    assert dict(after) == dict(before), "the candidate learned nothing from a local fault"
    execution = await workflow.pool.fetchrow("SELECT state,infrastructure_retries FROM executions")
    assert execution["state"] == "WAITING_RETRY"
    assert execution["infrastructure_retries"] == 1, "the budget that advanced is this side's own"

    # The same candidate is still what the next attempt reaches for.
    await workflow.pool.execute("UPDATE executions SET due_at=now()-interval '1 second'")
    assert (await workflow.next_activity()).identity == request.identity
