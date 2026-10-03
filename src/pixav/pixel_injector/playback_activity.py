"""Managed playback activity worker: materializes one playable object.

It reports what happened and nothing else. It cannot decide that an object is
READY, choose which asset to prepare, or schedule a retry -- those belong to
the execution authority. It does own the playback cache's contents: an object
is published into ``objects/`` only after its bytes were recomputed here and
matched a receipt that satisfies the materialization policy, so the resolver
never needs to trust anything it did not see verified.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Protocol

import asyncpg
from pydantic import ValidationError

from pixav.media_loader.video_parts import contained_file, sha256
from pixav.pixel_injector.photos_storage import COLD_MOUNTS, ColdProcessError, PhotosColdReadback
from pixav.shared.playable_assets import playable_from_row
from pixav.shared.playback_models import (
    CACHE_OBJECTS,
    CACHE_WORK,
    PlayableAsset,
    PlaybackInputs,
    cache_object_name,
    validate_materialization,
)
from pixav.shared.queue import TaskQueue
from pixav.shared.remote_assets import expectation, segment_from_row
from pixav.shared.storage_models import COLD_READBACK_METHOD, IntegrityError, RemoteAssetSegment
from pixav.shared.workflow import ActivityRequest, ActivityResult

logger = logging.getLogger(__name__)

# What the isolated process may say went wrong that is a statement about the
# bytes rather than about the environment. Everything else is infrastructure.
_SEGMENT_MISMATCH = {"OriginalMismatchError"}
_INTEGRITY = {"IntegrityError", "IncompatibleMediaError"}


class PlaybackMaterializer(Protocol):
    async def materialize(self, manifest: dict, work: Path, *, timeout_seconds: int) -> dict:
        """Run the isolated materialization in ``work`` and return its receipt."""
        ...


class PhotosPlaybackMaterializer(PhotosColdReadback):
    """The storage read-back's isolation, running the playback entry point."""

    ENTRY = "pixav.pixel_injector.playback_materialize"
    LABEL = "playback-materialize"

    async def materialize(self, manifest: dict, work: Path, *, timeout_seconds: int) -> dict:
        payload = json.dumps(manifest).encode()
        receipt = await asyncio.to_thread(self._run, work, payload, timeout_seconds=timeout_seconds)
        return {**receipt, "cold_mounts": list(COLD_MOUNTS)}


class PlaybackRefusedError(Exception):
    """The materialization is not the recorded object; carries a bounded code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _redacted(segment: RemoteAssetSegment) -> dict:
    """What the isolated process may know: the provider location and the facts."""
    clean = segment.model_copy(update={"local_path": "", "account_id": None, "recovery": {}, "verification": {}})
    return json.loads(clean.model_dump_json())


def _link_verified_readback(segment: RemoteAssetSegment, readback_root: Path, cloud: Path) -> bool:
    """Offer the storage read-back's copy of a segment, only if it still proves itself.

    The file must sit beside its original cold receipt, be a regular file inside
    the read-back root, and hash to the durable segment's digest now. Anything
    less and the segment is simply not offered, so the isolated process fetches
    it from Photos instead. Hard links cost no space and cannot be redirected.
    """
    folder = readback_root / str(segment.asset_id) / str(segment.segment_index)
    source, receipt = folder / segment.filename, folder / (segment.filename + ".json")
    try:
        contained_file(readback_root, source)
        contained_file(readback_root, receipt)
        report = json.loads(receipt.read_text())
        if (
            report.get("method") != COLD_READBACK_METHOD
            or report.get("size") != segment.size_bytes
            or report.get("sha256") != segment.sha256
            or source.stat().st_size != segment.size_bytes
            or sha256(source) != segment.sha256
        ):
            return False
        os.link(source, cloud / segment.filename)
        os.link(receipt, cloud / receipt.name)
    except (OSError, ValueError):
        for stray in (cloud / segment.filename, cloud / (segment.filename + ".json")):
            with suppress(FileNotFoundError):
                stray.unlink()
        return False
    return True


class PlaybackActivityWorker:
    """Claim, materialize, verify, publish, report."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        materializer: PlaybackMaterializer,
        *,
        cache_root: Path,
        readback_root: Path,
        segment_timeout_seconds: int = 21600,
    ) -> None:
        self.pool = pool
        self.materializer = materializer
        self.cache_root = cache_root
        self.readback_root = readback_root
        self.segment_timeout_seconds = segment_timeout_seconds

    def _fields(self, request: ActivityRequest) -> dict:
        return request.model_dump(exclude={"stage", "identity", "input_path", "share_url"})

    async def execute(self, request: ActivityRequest) -> ActivityResult:
        fields = self._fields(request)
        try:
            evidence = await self._materialize(request)
            return ActivityResult(**fields, outcome="success", evidence=evidence)
        except PlaybackRefusedError as refused:
            return ActivityResult(**fields, outcome="verification_failed", error_code=refused.code)
        except Exception:
            logger.exception("playback activity failed")
            return ActivityResult(**fields, outcome="infrastructure", error_code="PLAYBACK_MATERIALIZATION_FAILED")

    async def _load(self, request: ActivityRequest) -> tuple[PlayableAsset, dict, list[RemoteAssetSegment]]:
        row = await self.pool.fetchrow("SELECT * FROM playable_assets WHERE id=$1", request.playable_asset_id)
        if row is None:
            raise ValueError("the authority addressed a playable asset that does not exist")
        playable = playable_from_row(row)
        asset = await self.pool.fetchrow("SELECT expected FROM remote_assets WHERE id=$1", playable.remote_asset_id)
        segments = [
            segment_from_row(item)
            for item in await self.pool.fetch(
                "SELECT * FROM remote_asset_segments WHERE asset_id=$1 ORDER BY segment_index",
                playable.remote_asset_id,
            )
        ]
        if asset is None or not segments or request.asset_id != playable.remote_asset_id:
            raise ValueError("playable asset does not match the addressed remote asset")
        return playable, expectation(asset), segments

    async def _materialize(self, request: ActivityRequest) -> dict:
        playable, expected, segments = await self._load(request)
        work = self.cache_root / CACHE_WORK / str(playable.id) / str(request.attempt_id)
        # A fresh, empty directory per attempt: a rebuild must come from Photos,
        # never from bytes some earlier attempt left behind.
        work.mkdir(parents=True, exist_ok=False, mode=0o700)
        try:
            receipt, inputs = await self._run(request, playable, expected, segments, work)
            output = await asyncio.to_thread(self._verify_output, work, playable, receipt)
            await asyncio.to_thread(self._publish, output, playable.id, request.attempt_id)
        except PlaybackRefusedError:
            # Bytes that are not the film are evidence; keep them for inspection.
            raise
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, work, True)
            raise
        await asyncio.to_thread(shutil.rmtree, work, True)
        return {**receipt, "inputs": inputs}

    async def _run(self, request, playable, expected, segments, work) -> tuple[dict, PlaybackInputs]:
        cloud = work / "cloud"
        cloud.mkdir(mode=0o700)
        offered = 0
        if request.purpose == "prepare":
            for segment in segments:
                offered += await asyncio.to_thread(_link_verified_readback, segment, self.readback_root, cloud)
        inputs: PlaybackInputs = (
            "verified-readback" if offered == len(segments) else "photos" if offered == 0 else "mixed"
        )
        manifest = {
            "version": 1,
            "segments": [_redacted(segment) for segment in segments],
            "expected": expected,
            "output": cache_object_name(playable.id),
        }
        try:
            receipt = await self.materializer.materialize(
                manifest, work, timeout_seconds=self.segment_timeout_seconds * len(segments)
            )
        except ColdProcessError as failure:
            if failure.error_type in _SEGMENT_MISMATCH:
                raise PlaybackRefusedError("PLAYBACK_SEGMENT_MISMATCH") from None
            if failure.error_type in _INTEGRITY:
                raise PlaybackRefusedError("PLAYBACK_INTEGRITY_MISMATCH") from None
            raise
        try:
            validate_materialization(expected, segments, receipt)
        except IntegrityError:
            raise PlaybackRefusedError("PLAYBACK_INTEGRITY_MISMATCH") from None
        if request.purpose == "rebuild" and (receipt["size"], receipt["sha256"]) != (
            playable.size_bytes,
            playable.sha256,
        ):
            raise PlaybackRefusedError("PLAYBACK_REBUILD_MISMATCH")
        return receipt, inputs

    @staticmethod
    def _verify_output(work: Path, playable: PlayableAsset, receipt: dict) -> Path:
        """Recompute the object's identity here rather than trusting the child."""
        output = contained_file(work, work / cache_object_name(playable.id))
        if output.stat().st_size != receipt["size"] or sha256(output) != receipt["sha256"]:
            raise PlaybackRefusedError("PLAYBACK_INTEGRITY_MISMATCH")
        return output

    def _publish(self, output: Path, playable_id: uuid.UUID, attempt_id: uuid.UUID) -> None:
        """Make the verified object visible under its identity in one rename.

        A reader that already holds the previous object keeps its own open file;
        the rename only changes what the next open finds.
        """
        objects = self.cache_root / CACHE_OBJECTS
        objects.mkdir(parents=True, exist_ok=True, mode=0o700)
        if objects.is_symlink():
            raise ValueError("playback cache objects directory is a symlink")
        staged = objects / f".{attempt_id}.partial"
        os.link(output, staged)
        # Expiry counts unread time from publication, not from when a reused
        # read-back file (the same inode) was first written.
        os.utime(staged)
        os.replace(staged, objects / cache_object_name(playable_id))
        descriptor = os.open(objects, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def run_one(self, queue: TaskQueue) -> bool:
        claimed = await queue.pop_claim(timeout=1)
        if claimed is None:
            return False
        payload, receipt = claimed
        try:
            request = ActivityRequest.model_validate(payload)
        except ValidationError:
            logger.warning("invalid playback activity envelope")
            await queue.ack(receipt)
            return True
        if request.stage != "playback" or request.playable_asset_id is None or request.purpose is None:
            logger.warning("playback transport carried a non-playback activity")
            await queue.ack(receipt)
            return True
        token = uuid.uuid4()
        if not await self._matches_intent(request) or not await self.pool.fetchval(
            "SELECT claim_activity($1,$2)", request.attempt_id, token
        ):
            await queue.ack(receipt)
            return True
        running = asyncio.create_task(self.execute(request))
        heartbeat = asyncio.create_task(self._heartbeat(request, token, running))
        try:
            try:
                result = await running
            except asyncio.CancelledError:
                if not heartbeat.done():
                    raise
                # The lease moved on. Whatever this attempt would have reported
                # belongs to a generation the authority no longer accepts.
                logger.warning("playback lease lost; attempt abandoned")
                await queue.ack(receipt)
                return True
            await self.pool.fetchval("SELECT report_activity($1::jsonb)", result.model_dump_json())
            await queue.ack(receipt)
        finally:
            heartbeat.cancel()
            running.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            with suppress(asyncio.CancelledError):
                await running
        return True

    async def _matches_intent(self, request: ActivityRequest) -> bool:
        """The envelope must describe the intent the authority actually stored."""
        row = await self.pool.fetchrow(
            """SELECT e.task_id,e.owner,e.generation,e.stage,a.operation_id,o.identity,e.checkpoint
            FROM executions e JOIN activity_attempts a ON a.execution_id=e.id
            JOIN operation_intents o ON o.id=a.operation_id WHERE e.id=$1 AND a.id=$2
            AND a.generation=e.generation AND e.state='RUNNING' AND e.lease_until > now()""",
            request.execution_id,
            request.attempt_id,
        )
        if row is None:
            return False
        checkpoint = json.loads(row["checkpoint"])
        expected = (
            row["task_id"],
            row["owner"],
            row["generation"],
            row["stage"],
            row["operation_id"],
            row["identity"],
            checkpoint.get("playable_asset_id"),
            checkpoint.get("purpose"),
        )
        observed = (
            request.task_id,
            request.owner,
            request.generation,
            request.stage,
            request.operation_id,
            request.identity,
            str(request.playable_asset_id),
            request.purpose,
        )
        if expected != observed:
            logger.warning("playback envelope does not match persisted intent")
            return False
        return True

    async def _heartbeat(self, request: ActivityRequest, token: uuid.UUID, running: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(15)  # asyncio uses a monotonic clock
            try:
                owned = await self.pool.fetchval("SELECT heartbeat_activity($1,$2)", request.attempt_id, token)
            except Exception:
                owned = False
            if not owned:
                running.cancel()
                return
