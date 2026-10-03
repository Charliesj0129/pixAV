"""Real PostgreSQL PlayableAsset lifecycle driven through the one authority.

Worker results are reported through the same ``report_activity`` function a
real playback worker uses. Nothing here shows that a Photos download or a
real player succeeded; it shows what pixAV commits when they report.
"""

import hashlib
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from pixav.maxwell_core.media_workflow import MediaWorkflow
from pixav.shared.playback_models import MATERIALIZATION_METHOD
from pixav.shared.remote_assets import RemoteAssetRepository
from pixav.shared.workflow import ActivityResult

pytestmark = pytest.mark.integration

CONTENT = b"synthetic whole film"
DIGEST = hashlib.sha256(CONTENT).hexdigest()
FACTS = {
    "container": "mov,mp4",
    "size_bytes": len(CONTENT),
    "duration_seconds": 10.0,
    "sha256": DIGEST,
    "streams": [
        {"kind": "video", "codec": "h264", "width": 1920, "height": 1080},
        {"kind": "audio", "codec": "aac", "width": 0, "height": 0},
    ],
}


@pytest.fixture
async def workflow(integration_db):
    for path in sorted(Path("migrations").glob("*.sql")):
        await integration_db.execute(path.read_text())
    return MediaWorkflow(integration_db, backoff=(1, 2), lease_seconds=120)


async def durable_video(db):
    """A video whose single-segment remote asset is DURABLE and whose task is done."""
    video = await db.fetchval("INSERT INTO videos(title) VALUES('synthetic fixture') RETURNING id")
    task, execution, operation, artifact, asset = (uuid.uuid4() for _ in range(5))
    await db.execute(
        "INSERT INTO tasks(id,video_id,queue_name,state) VALUES($1,$2,'pixav:media-managed','complete')", task, video
    )
    await db.execute("INSERT INTO workflow_tasks(task_id,video_id) VALUES($1,$2)", task, video)
    await db.execute(
        "INSERT INTO executions(id,task_id,max_retries,state,stage) VALUES($1,$2,1,'SUCCEEDED','verify')",
        execution,
        task,
    )
    await db.execute(
        "INSERT INTO operation_intents(id,execution_id,stage,identity) VALUES($1,$2,'prepare','synthetic')",
        operation,
        execution,
    )
    await db.execute(
        "INSERT INTO workflow_artifacts(id,execution_id,operation_id,path,facts) VALUES($1,$2,$3,'/staging/x','{}')",
        artifact,
        execution,
        operation,
    )
    await db.execute(
        """INSERT INTO remote_assets(id,video_id,artifact_id,policy_version,expected,segment_count,state,durable_at)
        VALUES($1,$2,$3,'photos-original-v2',$4::jsonb,1,'DURABLE',now())""",
        asset,
        video,
        artifact,
        json.dumps(FACTS),
    )
    await db.execute(
        """INSERT INTO remote_asset_segments(asset_id,segment_index,start_seconds,end_seconds,size_bytes,sha256,
        local_path,media_info,share_url,usage_counted_at,state)
        VALUES($1,0,0,10,$2,$3,'/staging/x','{}','https://photos.app.goo.gl/synthetic',now(),'verified')""",
        asset,
        len(CONTENT),
        DIGEST,
    )
    return video, asset


def receipt(*, sha256: str = DIGEST, inputs: str = "verified-readback") -> dict:
    return {
        "method": MATERIALIZATION_METHOD,
        "segments": [{"index": 0, "size": len(CONTENT), "sha256": DIGEST, "method": "photos-original-browser"}],
        "junctions": "single-segment",
        "observed": {**FACTS, "sha256": sha256},
        "size": len(CONTENT),
        "sha256": sha256,
        "inputs": inputs,
    }


async def report(workflow, request, outcome="success", **kwargs):
    fields = request.model_dump(exclude={"stage", "identity", "input_path", "share_url"})
    result = ActivityResult(**fields, outcome=outcome, **kwargs)
    assert await workflow.pool.fetchval("SELECT report_activity($1::jsonb)", result.model_dump_json())
    await workflow.consume_results()


async def playable(workflow, video):
    return await workflow.pool.fetch("SELECT * FROM playable_assets WHERE video_id=$1 ORDER BY created_at", video)


async def ready(workflow):
    video, asset = await durable_video(workflow.pool)
    await workflow.playback.open_due()
    request = await workflow.next_activity()
    await report(workflow, request, evidence=receipt())
    return video, asset


async def test_durable_asset_becomes_ready_through_one_execution_bdd_066(workflow):
    video, asset = await durable_video(workflow.pool)

    opened = await workflow.playback.open_due()
    request = await workflow.next_activity()

    assert len(opened) == 1 and request.stage == "playback" and request.purpose == "prepare"
    assert request.asset_id == asset
    rows = await playable(workflow, video)
    assert [row["state"] for row in rows] == ["PREPARING"] and request.playable_asset_id == rows[0]["id"]

    await report(workflow, request, evidence=receipt())

    row = (await playable(workflow, video))[0]
    assert row["state"] == "READY" and row["sha256"] == DIGEST and row["size_bytes"] == len(CONTENT)
    assert json.loads(row["evidence"])["preparation"]["inputs"] == "verified-readback"
    assert await workflow.pool.fetchval("SELECT state FROM executions WHERE id=$1", request.execution_id) == "SUCCEEDED"
    assert await workflow.playback.open_due() == []


async def test_not_durable_asset_is_never_prepared_bdd_067_086(workflow):
    video, _ = await durable_video(workflow.pool)
    await workflow.pool.execute("UPDATE remote_assets SET state='VERIFIED', durable_at=NULL")

    assert await workflow.playback.open_due() == []
    assert await playable(workflow, video) == []


async def test_materialization_of_other_bytes_never_becomes_ready_bdd_069_084(workflow):
    video, _ = await durable_video(workflow.pool)
    await workflow.playback.open_due()
    request = await workflow.next_activity()

    await report(workflow, request, evidence=receipt(sha256="f" * 64))

    row = (await playable(workflow, video))[0]
    assert row["state"] == "INVALID" and row["invalidated_reason"] == "PLAYBACK_INTEGRITY_MISMATCH"
    execution = await workflow.pool.fetchrow("SELECT * FROM executions WHERE id=$1", request.execution_id)
    assert execution["state"] == "FAILED" and execution["blocked_reason"] == "PLAYBACK_INTEGRITY_MISMATCH"
    # The same recipe cannot be expected to produce other bytes next time.
    assert await workflow.playback.open_due() == []


async def test_missing_segment_report_leaves_nothing_ready_bdd_084(workflow):
    video, _ = await durable_video(workflow.pool)
    await workflow.playback.open_due()
    request = await workflow.next_activity()

    await report(workflow, request, "verification_failed", error_code="PLAYBACK_SEGMENT_MISMATCH")

    assert [row["state"] for row in await playable(workflow, video)] == ["INVALID"]
    assert await workflow.pool.fetchval("SELECT state FROM remote_assets") == "DURABLE"


async def test_download_failure_retries_without_touching_the_remote_asset_bdd_084(workflow):
    video, _ = await durable_video(workflow.pool)
    await workflow.playback.open_due()
    request = await workflow.next_activity()

    await report(workflow, request, "infrastructure", error_code="PLAYBACK_MATERIALIZATION_FAILED")

    execution = await workflow.pool.fetchrow("SELECT * FROM executions WHERE id=$1", request.execution_id)
    assert execution["state"] == "WAITING_RETRY"
    assert [row["state"] for row in await playable(workflow, video)] == ["PREPARING"]
    assert await workflow.pool.fetchval("SELECT state FROM remote_assets") == "DURABLE"


async def test_cache_demand_opens_one_rebuild_that_must_reproduce_the_bytes_bdd_081(workflow):
    video, _ = await ready(workflow)
    row = (await playable(workflow, video))[0]
    assert await workflow.playback.open_due() == []

    await workflow.pool.execute("INSERT INTO playback_cache_requests(playable_asset_id) VALUES($1)", row["id"])
    opened = await workflow.playback.open_due()
    request = await workflow.next_activity()

    assert len(opened) == 1 and request.purpose == "rebuild" and request.playable_asset_id == row["id"]
    await report(workflow, request, evidence=receipt(inputs="photos"))

    after = (await playable(workflow, video))[0]
    assert after["state"] == "READY" and after["materialized_at"] > row["materialized_at"]
    assert json.loads(after["evidence"])["rebuild"]["inputs"] == "photos"
    # The demand has been answered; nothing reopens until a reader asks again.
    assert await workflow.playback.open_due() == []


async def test_rebuild_that_cannot_reproduce_the_object_makes_it_stale(workflow):
    video, _ = await ready(workflow)
    row = (await playable(workflow, video))[0]
    await workflow.pool.execute("INSERT INTO playback_cache_requests(playable_asset_id) VALUES($1)", row["id"])
    await workflow.playback.open_due()
    request = await workflow.next_activity()

    await report(workflow, request, "verification_failed", error_code="PLAYBACK_REBUILD_MISMATCH")

    assert [r["state"] for r in await playable(workflow, video)] == ["STALE"]
    # A stale object is replaced by a fresh preparation of the same remote asset.
    await workflow.playback.open_due()
    assert [r["state"] for r in await playable(workflow, video)] == ["STALE", "PREPARING"]


async def test_failed_rebuild_waits_for_new_demand(workflow):
    video, _ = await ready(workflow)
    row = (await playable(workflow, video))[0]
    await workflow.pool.execute("INSERT INTO playback_cache_requests(playable_asset_id) VALUES($1)", row["id"])
    await workflow.playback.open_due()
    request = await workflow.next_activity()
    await report(workflow, request, "verification_failed", error_code="PLAYBACK_SEGMENT_MISMATCH")

    assert (await playable(workflow, video))[0]["state"] == "READY"
    assert await workflow.playback.open_due() == []
    await workflow.pool.execute("UPDATE playback_cache_requests SET requested_at=now(), request_count=request_count+1")
    assert len(await workflow.playback.open_due()) == 1


async def test_withdrawn_remote_copy_closes_a_waiting_preparation(workflow):
    video, asset = await durable_video(workflow.pool)
    opened = await workflow.playback.open_due()

    await RemoteAssetRepository(workflow.pool).invalidate(asset, "OPERATOR_CONFIRMED")

    assert await workflow.next_activity() is None
    execution = await workflow.pool.fetchrow("SELECT * FROM executions WHERE id=$1", opened[0])
    assert execution["state"] == "FAILED" and execution["blocked_reason"] == "PLAYBACK_NOT_ELIGIBLE"
    assert [r["state"] for r in await playable(workflow, video)] == ["INVALID"]


async def test_newer_durable_asset_supersedes_the_ready_object(workflow):
    video, first = await ready(workflow)
    task = await workflow.pool.fetchval("SELECT task_id FROM workflow_tasks WHERE video_id=$1", video)
    execution = await workflow.pool.fetchval("SELECT id FROM executions WHERE task_id=$1 LIMIT 1", task)
    operation = await workflow.pool.fetchval(
        "INSERT INTO operation_intents(id,execution_id,stage,identity) VALUES($1,$2,'prepare','second') RETURNING id",
        uuid.uuid4(),
        execution,
    )
    artifact = await workflow.pool.fetchval(
        "INSERT INTO workflow_artifacts(id,execution_id,operation_id,path,facts) VALUES($1,$2,$3,'/staging/y','{}')"
        " RETURNING id",
        uuid.uuid4(),
        execution,
        operation,
    )
    second = await workflow.pool.fetchval(
        """INSERT INTO remote_assets(id,video_id,artifact_id,policy_version,expected,segment_count,state,durable_at)
        VALUES($1,$2,$3,'photos-original-v2',$4::jsonb,1,'DURABLE',now()) RETURNING id""",
        uuid.uuid4(),
        video,
        artifact,
        json.dumps(FACTS),
    )
    await workflow.pool.execute(
        """INSERT INTO remote_asset_segments(asset_id,segment_index,start_seconds,end_seconds,size_bytes,sha256,
        local_path,media_info,share_url,usage_counted_at,state)
        VALUES($1,0,0,10,$2,$3,'/staging/y','{}','https://photos.app.goo.gl/other',now(),'verified')""",
        second,
        len(CONTENT),
        DIGEST,
    )

    await workflow.playback.open_due()
    request = await workflow.next_activity()
    await report(workflow, request, evidence=receipt())

    rows = await playable(workflow, video)
    assert [(r["remote_asset_id"], r["state"]) for r in rows] == [(first, "STALE"), (second, "READY")]
    # The older copy is not prepared again just because its object went stale.
    assert await workflow.playback.open_due() == []


async def test_roles_separate_reading_demand_and_committing(workflow):
    video, _ = await ready(workflow)
    row = (await playable(workflow, video))[0]
    async with workflow.pool.acquire() as conn:
        await conn.execute("SET ROLE pixav_playback_reader")
        try:
            assert await conn.fetchval("SELECT state FROM playable_assets WHERE id=$1", row["id"]) == "READY"
            await conn.execute("INSERT INTO playback_cache_requests(playable_asset_id) VALUES($1)", row["id"])
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("UPDATE playable_assets SET state='READY' WHERE id=$1", row["id"])
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("UPDATE remote_assets SET state='DURABLE'")
        finally:
            await conn.execute("RESET ROLE")
        await conn.execute("SET ROLE pixav_activity_worker")
        try:
            assert await conn.fetchval("SELECT count(*) FROM playable_assets") == 1
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("UPDATE playable_assets SET state='READY' WHERE id=$1", row["id"])
        finally:
            await conn.execute("RESET ROLE")


async def test_authority_role_can_run_the_playback_transitions(workflow):
    """The executable SQL must work under the production role, not just admin."""
    video, _ = await durable_video(workflow.pool)

    async def set_role(conn):
        await conn.execute("SET ROLE pixav_execution_authority")

    database = await workflow.pool.fetchval("SELECT current_database()")
    endpoint = urlsplit(os.environ["PIXAV_E2E_ADMIN_DSN"])
    pool = await asyncpg.create_pool(
        dsn=urlunsplit(endpoint._replace(path=f"/{database}")), setup=set_role, min_size=1, max_size=2
    )
    try:
        authority = MediaWorkflow(pool)
        assert len(await authority.playback.open_due()) == 1
        request = await authority.next_activity()
        await report(authority, request, evidence=receipt())
        row = (await playable(authority, video))[0]
        assert row["state"] == "READY"
        await workflow.pool.execute("INSERT INTO playback_cache_requests(playable_asset_id) VALUES($1)", row["id"])
        assert len(await authority.playback.open_due()) == 1
        assert await authority.playback.observe() == {"READY": 1}
    finally:
        await pool.close()
