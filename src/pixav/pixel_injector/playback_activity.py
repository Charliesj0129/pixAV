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
import errno
import json
import logging
import os
import shutil
import uuid
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Protocol

import asyncpg
from pydantic import ValidationError

from pixav.media_loader.video_parts import contained_file, sha256
from pixav.pixel_injector.photos_auth import AccountSessionRequiredError
from pixav.pixel_injector.photos_storage import COLD_MOUNTS, ColdProcessError, PhotosColdReadback
from pixav.shared.activity_context import activity_context
from pixav.shared.media_file import open_media_directory, open_media_file
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
        if self.requires_fresh_work:
            receipt = await self.run_authenticated(manifest, work, timeout_seconds)
        else:
            payload = json.dumps(manifest).encode()
            receipt = await asyncio.to_thread(self._run, work, payload, timeout_seconds=timeout_seconds)
        return {**receipt, "cold_mounts": list(COLD_MOUNTS)}


class PlaybackRefusedError(Exception):
    """The materialization is not the recorded object; carries a bounded code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _redacted(segment: RemoteAssetSegment, *, account_bound: bool = False) -> dict:
    """What the isolated process may know: the provider location and the facts."""
    clean = segment.model_copy(
        update={
            "local_path": "",
            "account_id": segment.account_id if account_bound else None,
            "recovery": {},
            "verification": {},
        }
    )
    return json.loads(clean.model_dump_json())


def _link_verified_readback(segment: RemoteAssetSegment, readback_root: Path, cloud: Path) -> bool:
    """Offer the storage read-back's copy of a segment, only if it still proves itself.

    The file must sit beside its original cold receipt, be a regular file inside
    the read-back root, and hash to the durable segment's digest now. Anything
    less and the segment is simply not offered, so the isolated process fetches
    it from Photos instead. Fresh-session directories and legacy receipts are
    supported. Separate Docker mounts may prohibit hard links; then a private
    copy is hashed again before it is offered. Rebuild never calls this helper.
    """
    folder = readback_root / str(segment.asset_id) / str(segment.segment_index)
    # Inspect only this segment's immediate, bounded session directories. Do
    # not follow a symlink or search staging for a similarly named original.
    folders = [folder]
    try:
        if folder.is_symlink():
            return False
        for child in folder.iterdir():
            if child.name.startswith("session-") and child.is_dir() and not child.is_symlink():
                folders.append(child)
                if len(folders) > 65:
                    return False
    except OSError:
        return False
    for candidate in sorted(folders, key=lambda p: p.name, reverse=True):
        if _offer_readback(segment, readback_root, candidate, cloud):
            return True
    return False


def _offer_readback(segment: RemoteAssetSegment, root: Path, folder: Path, cloud: Path) -> bool:
    source, receipt = folder / segment.filename, folder / (segment.filename + ".json")
    try:
        contained_file(root, source)
        contained_file(root, receipt)
        report = json.loads(receipt.read_text())
        if (
            report.get("method") != COLD_READBACK_METHOD
            or report.get("size") != segment.size_bytes
            or report.get("sha256") != segment.sha256
            or source.stat().st_size != segment.size_bytes
            or sha256(source) != segment.sha256
        ):
            return False
        for original in (source, receipt):
            destination = cloud / original.name
            try:
                os.link(original, destination)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with original.open("rb") as reader, os.fdopen(fd, "wb") as writer:
                    shutil.copyfileobj(reader, writer)
                    writer.flush()
                    os.fsync(writer.fileno())
        if (cloud / segment.filename).stat().st_size != segment.size_bytes or sha256(
            cloud / segment.filename
        ) != segment.sha256:
            raise ValueError("read-back changed while it was offered")
        if json.loads((cloud / receipt.name).read_text()) != report:
            raise ValueError("read-back receipt changed while it was offered")
    except (OSError, ValueError, UnicodeError):
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
        except AccountSessionRequiredError:
            return ActivityResult(**fields, outcome="user_action", error_code="PHOTOS_ACCOUNT_SESSION_REQUIRED")
        except Exception:
            logger.error("playback activity failed")
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
            await asyncio.to_thread(
                self._publish, output, playable.id, request.attempt_id, (receipt["sha256"], int(receipt["size"]))
            )
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
        if request.purpose == "prepare" and not getattr(self.materializer, "requires_fresh_work", False):
            for segment in segments:
                offered += await asyncio.to_thread(_link_verified_readback, segment, self.readback_root, cloud)
        inputs: PlaybackInputs = (
            "verified-readback" if offered == len(segments) else "photos" if offered == 0 else "mixed"
        )
        manifest = {
            "version": 1,
            "segments": [
                _redacted(segment, account_bound=bool(getattr(self.materializer, "requires_fresh_work", False)))
                for segment in segments
            ],
            "expected": expected,
            "output": cache_object_name(playable.id),
        }
        try:
            receipt = await self.materializer.materialize(
                manifest, work, timeout_seconds=self.segment_timeout_seconds * len(segments)
            )
        except ColdProcessError as failure:
            if failure.error_type in {"AccountSessionRequiredError", "AccountSessionMismatchError"}:
                raise AccountSessionRequiredError("Photos session requires operator login") from None
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

    def _publish(
        self, output: Path, playable_id: uuid.UUID, attempt_id: uuid.UUID, expected: tuple[str, int] | None = None
    ) -> None:
        """Publish verified bytes while coordinating existing readers and eviction."""
        objects = self.cache_root / CACHE_OBJECTS
        objects.mkdir(parents=True, exist_ok=True, mode=0o700)
        if objects.is_symlink():
            raise ValueError("playback cache objects directory is a symlink")
        staged = objects / f".{attempt_id}.partial"
        target = objects / cache_object_name(playable_id)
        with open_media_file(output) as source:
            if expected is not None and source.facts() != expected:
                raise PlaybackRefusedError("PLAYBACK_INTEGRITY_MISMATCH")
            with open_media_directory(target, exclusive=True) as directory, ExitStack() as guards:
                source_info = os.fstat(source.descriptor)
                try:
                    existing = os.stat(target.name, dir_fd=directory.descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if (existing.st_dev, existing.st_ino) == (source_info.st_dev, source_info.st_ino):
                        return
                    guards.enter_context(open_media_file(target, exclusive=True, directory=directory))
                created = False
                try:
                    source.assert_current()
                    os.link(
                        source.path.name,
                        staged.name,
                        src_dir_fd=source.parent_descriptor,
                        dst_dir_fd=directory.descriptor,
                        follow_symlinks=False,
                    )
                    created = True
                    linked = os.stat(staged.name, dir_fd=directory.descriptor, follow_symlinks=False)
                    if (linked.st_dev, linked.st_ino) != (source_info.st_dev, source_info.st_ino):
                        raise PlaybackRefusedError("PLAYBACK_INTEGRITY_MISMATCH")
                    os.utime(staged.name, dir_fd=directory.descriptor, follow_symlinks=False)
                    directory.assert_current()
                    os.replace(
                        staged.name, target.name, src_dir_fd=directory.descriptor, dst_dir_fd=directory.descriptor
                    )
                    os.fsync(directory.descriptor)
                finally:
                    if created:
                        with suppress(FileNotFoundError):
                            os.unlink(staged.name, dir_fd=directory.descriptor)

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
        matched = await self._matches_intent(request)
        if not matched or not await self.pool.fetchval("SELECT claim_activity($1,$2)", request.attempt_id, token):
            logger.warning(
                "playback attempt refused: intent_match=%s generation=%s attempt=%s",
                matched,
                request.generation,
                request.attempt_id,
            )
            await queue.ack(receipt)
            return True
        with activity_context(request, token):
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
