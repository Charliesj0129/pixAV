"""Media activity boundaries: persisted intent, lease loss and durable reporting."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from pixav.media_loader.activity import MediaActivityWorker
from pixav.media_loader.interfaces import DownloadObservation
from pixav.media_loader.preparation import MediaFacts, PreparedArtifact
from pixav.shared.exceptions import MediaDependencyError, RemuxError, SourceUnavailableError, TorrentOwnershipError
from pixav.shared.workflow import ActivityRequest, ActivityResult


def request(**changes):
    fields = dict(
        task_id=uuid4(),
        execution_id=uuid4(),
        attempt_id=uuid4(),
        operation_id=uuid4(),
        owner=uuid4(),
        generation=1,
        stage="download",
        identity="a" * 40,
    )
    return ActivityRequest(**{**fields, **changes})


def worker_for(command, tmp_path):
    pool, client, queue = AsyncMock(), AsyncMock(), AsyncMock()
    pool.fetchrow.return_value = {
        **command.model_dump(),
        "checkpoint": json.dumps({"download_path": command.input_path}),
    }
    pool.fetchval.return_value = True
    queue.pop_claim.return_value = (command.model_dump(mode="json"), "receipt")
    worker = MediaActivityWorker(pool, client, AsyncMock(), output_dir=str(tmp_path))
    return worker, pool, client, queue


@pytest.mark.parametrize("symlink", [False, True])
async def test_completed_artifact_missing_from_worker_mount_is_dependency_fault(tmp_path, symlink):
    command = request()
    worker, pool, client, _ = worker_for(command, tmp_path)
    media = tmp_path / "missing.mp4"
    if symlink:
        media.symlink_to(tmp_path / "absent.mp4")
    client.check_download.return_value = DownloadObservation(progress=1, downloaded_bytes=256, artifact_path=str(media))
    result = await worker.execute(command)
    assert result.operation_id == command.operation_id
    assert result.outcome == ("invalid_media" if symlink else "infrastructure")
    assert result.error_code == ("INVALID_MEDIA" if symlink else "MEDIA_DEPENDENCY")
    assert len(pool.mock_calls) == 1


@pytest.mark.parametrize(
    "failure,outcome",
    [
        (SourceUnavailableError(), "source_unavailable"),
        (TorrentOwnershipError(), "unknown_effect"),
        (MediaDependencyError(), "infrastructure"),
        (RemuxError(), "invalid_media"),
        (ConnectionError(), "infrastructure"),
    ],
)
async def test_failure_classification_never_schedules_retry_bdd_020_030_031_032(tmp_path, failure, outcome):
    command = request()
    worker, pool, client, _ = worker_for(command, tmp_path)
    client.check_download.side_effect = failure
    result = await worker.execute(command)
    assert result.outcome == outcome
    assert len(pool.mock_calls) == 1  # read-only first-attempt check
    client.delete_torrent.assert_not_awaited()


@pytest.mark.parametrize("kind", ["invalid", "wrong_stage", "stale", "tampered", "already_claimed"])
async def test_unowned_envelopes_cannot_start_side_effects_bdd_019(tmp_path, kind):
    command = request()
    worker, pool, client, queue = worker_for(command, tmp_path)
    if kind == "invalid":
        queue.pop_claim.return_value = ({"task_id": "bad"}, "receipt")
    elif kind == "wrong_stage":
        queue.pop_claim.return_value = (request(stage="upload").model_dump(mode="json"), "receipt")
    elif kind == "stale":
        pool.fetchrow.return_value = None
    elif kind == "tampered":
        pool.fetchrow.return_value["identity"] = "b" * 40
    else:
        pool.fetchval.return_value = False
    assert await worker.run_one(queue)
    client.check_download.assert_not_awaited()
    queue.ack.assert_awaited_once_with("receipt")


async def test_result_commit_failure_keeps_receipt_for_authority_recovery_bdd_003_034(tmp_path):
    command = request()
    worker, pool, client, queue = worker_for(command, tmp_path)
    media = tmp_path / "synthetic.mp4"
    media.write_bytes(b"downloaded bytes")
    client.check_download.return_value = DownloadObservation(progress=1, downloaded_bytes=16, artifact_path=str(media))
    pool.fetchval.side_effect = [True, ConnectionError("database unavailable")]
    assert await worker.run_one(queue)
    queue.ack.assert_not_awaited()
    queue.push.assert_not_awaited()
    client.delete_torrent.assert_not_awaited()


async def test_lease_loss_cancels_activity_without_killing_worker_bdd_019(tmp_path):
    command = request()
    worker, _, client, queue = worker_for(command, tmp_path)
    started = asyncio.Event()

    async def downloading(*args, **kwargs):
        started.set()
        await asyncio.Future()

    client.check_download.side_effect = downloading

    async def lose_lease(request, token, running):
        await started.wait()
        running.cancel()

    worker._heartbeat = lose_lease
    assert await worker.run_one(queue)
    queue.ack.assert_not_awaited()
    queue.push.assert_not_awaited()


async def test_shutdown_cancellation_propagates_and_retains_receipt(tmp_path):
    command = request()
    worker, _, client, queue = worker_for(command, tmp_path)
    started = asyncio.Event()

    async def downloading(*args, **kwargs):
        started.set()
        await asyncio.Future()

    client.check_download.side_effect = downloading
    running = asyncio.create_task(worker.run_one(queue))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    queue.ack.assert_not_awaited()


async def test_download_pause_does_not_execute_or_create_retry(tmp_path):
    command = request()
    worker, pool, client, queue = worker_for(command, tmp_path)
    assert not await worker.run_one(queue, download_paused=True)
    assert not pool.mock_calls
    client.check_download.assert_not_awaited()
    queue.nack.assert_awaited_once_with("receipt", requeue=True)


@pytest.mark.parametrize("changed", [False, True])
async def test_revalidated_preparation_is_bound_to_the_retained_input(tmp_path, changed):
    command = request(stage="prepare", input_path=str(tmp_path / "synthetic.mp4"))
    worker, pool, _, queue = worker_for(command, tmp_path)
    pool.fetchrow.return_value["checkpoint"] = json.dumps(
        {"download_path": command.input_path, "preparation_revalidation": [{"input_sha256": "a" * 64}]}
    )
    facts = MediaFacts(
        container="mp4",
        size_bytes=100,
        duration_seconds=10,
        sha256=("b" if changed else "a") * 64,
        streams=({"kind": "video", "codec": "h264", "width": 1920, "height": 1080}, {"kind": "audio", "codec": "aac"}),
    )
    prepared = PreparedArtifact(
        path=command.input_path,
        input_path=command.input_path,
        input_sha256=facts.sha256,
        input_facts=facts,
        facts=facts,
    )
    worker.execute = AsyncMock(
        return_value=ActivityResult(
            **command.model_dump(exclude={"stage", "identity", "input_path"}), outcome="success", prepared=prepared
        )
    )
    assert await worker.run_one(queue)
    result = ActivityResult.model_validate_json(pool.fetchval.call_args.args[1])
    assert result.outcome == ("unknown_effect" if changed else "success")
    assert result.error_code == ("PREPARATION_INPUT_CHANGED" if changed else None)
    queue.ack.assert_awaited_once_with("receipt")
