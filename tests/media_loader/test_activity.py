"""Media activity boundaries: persisted intent, lease loss and durable reporting."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from pixav.media_loader.activity import MediaActivityWorker
from pixav.shared.exceptions import MediaDependencyError, RemuxError, SourceUnavailableError, TorrentOwnershipError
from pixav.shared.workflow import ActivityRequest


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
    client.reconcile_download.side_effect = failure
    result = await worker.execute(command)
    assert result.outcome == outcome
    assert not pool.mock_calls
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
    client.reconcile_download.assert_not_awaited()
    queue.ack.assert_awaited_once_with("receipt")


async def test_result_commit_failure_keeps_receipt_for_authority_recovery_bdd_003_034(tmp_path):
    command = request()
    worker, pool, client, queue = worker_for(command, tmp_path)
    media = tmp_path / "synthetic.mp4"
    media.write_bytes(b"downloaded bytes")
    client.reconcile_download.return_value = str(media)
    pool.fetchval.side_effect = [True, ConnectionError("database unavailable")]
    assert await worker.run_one(queue)
    queue.ack.assert_not_awaited()
    queue.push.assert_not_awaited()
    client.delete_torrent.assert_not_awaited()


async def test_lease_loss_cancels_activity_without_killing_worker_bdd_019(tmp_path):
    command = request()
    worker, _, client, queue = worker_for(command, tmp_path)
    started = asyncio.Event()

    async def downloading(*args):
        started.set()
        await asyncio.Future()

    client.reconcile_download.side_effect = downloading

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

    async def downloading(*args):
        started.set()
        await asyncio.Future()

    client.reconcile_download.side_effect = downloading
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
    client.reconcile_download.assert_not_awaited()
    queue.nack.assert_awaited_once_with("receipt", requeue=True)
