#!/usr/bin/env python3
"""Inspect or process exactly one guarded Phase 0 backlog item."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pixav.config import Settings, get_settings
from pixav.maxwell_core.dispatcher import RedisTaskDispatcher
from pixav.maxwell_core.gc import safe_cleanup_candidate
from pixav.media_loader.metadata import probe_media
from pixav.media_loader.qbittorrent import QBitClient
from pixav.media_loader.worker import run_loop
from pixav.pixel_injector.worker import run_from_settings as run_pixel_worker
from pixav.shared.db import create_pool
from pixav.shared.enums import TaskState, VideoStatus
from pixav.shared.pause import is_paused_value
from pixav.shared.queue import TaskQueue
from pixav.shared.redis_client import create_redis
from pixav.shared.repository import TaskRepository

if __package__:
    from scripts.backup_files import create_backup_file
    from scripts.instance_guard import database_identity, metadata_path, redis_identity, require_same_instance
    from scripts.phase0_backup import save_selected, validate_selected
else:  # Support the documented ``python scripts/phase0_backlog.py`` form.
    from backup_files import create_backup_file
    from instance_guard import database_identity, metadata_path, redis_identity, require_same_instance
    from phase0_backup import save_selected, validate_selected

_DEFAULT_EVIDENCE_DIR = Path("data/phase0/backlog")
_SAFE_ERROR_CLASSES = frozenset(
    {
        "DownloadError",
        "SourceUnavailableError",
        "RemuxError",
        "DatabaseError",
        "RedisError",
        "QueueError",
        "UploadError",
        "VerificationError",
        "AdbError",
        "RedroidError",
        "TimeoutError",
        "OSError",
        "RuntimeError",
        "ValueError",
    }
)
_SAFE_QUEUE_FIELDS = frozenset(
    {
        "account_id",
        "attempts",
        "failed_at",
        "max_retries",
        "queue_name",
        "retries",
        "stage",
        "task_id",
        "trace_id",
        "video_id",
    }
)


def _decode_object(raw: Any, *, context: str) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="strict")
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"{context} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{context} is not a JSON object")
    return payload


def _parse_expected_uuid(value: str, *, flag: str) -> uuid.UUID:
    if not value:
        raise RuntimeError(f"operation requires {flag}")
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise RuntimeError(f"{flag} must be a UUID") from exc


def _payload_ids(payload: dict[str, Any]) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    def _parse(value: Any) -> uuid.UUID | None:
        if not isinstance(value, str):
            return None
        try:
            return uuid.UUID(value)
        except ValueError:
            return None

    return _parse(payload.get("task_id")), _parse(payload.get("video_id"))


async def _effective_queue_head(redis: Any, queue_name: str) -> tuple[dict[str, Any], str] | None:
    """Read the item a restarted durable worker will claim next, without moving it."""
    raw = await redis.lindex(f"{queue_name}:processing", 0)
    source = "processing"
    if raw is None:
        raw = await redis.lindex(queue_name, 0)
        source = "queued"
    if raw is None:
        return None
    return _decode_object(raw, context=f"{queue_name} {source} head"), source


def _assert_queue_target(
    head: tuple[dict[str, Any], str] | None,
    *,
    queue_name: str,
    expected_task_id: uuid.UUID,
    expected_video_id: uuid.UUID,
) -> None:
    if head is None:
        raise RuntimeError(f"{queue_name} has no queued or recoverable processing payload")
    payload, source = head
    task_id, video_id = _payload_ids(payload)
    if task_id != expected_task_id or video_id != expected_video_id:
        raise RuntimeError(
            f"{queue_name} {source} head mismatch: "
            f"expected task={expected_task_id} video={expected_video_id}, "
            f"observed task={task_id} video={video_id}"
        )


def _sanitize_queue_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = {key: payload[key] for key in sorted(_SAFE_QUEUE_FIELDS) if key in payload}
    unknown = sorted(str(key) for key in payload if key not in _SAFE_QUEUE_FIELDS)
    if unknown:
        sanitized["redacted_fields"] = unknown
    return sanitized


async def _queue_snapshot(redis: Any, queue_name: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label, key in (("queued", queue_name), ("processing", f"{queue_name}:processing")):
        raw_items = await redis.lrange(key, 0, -1)
        result[label] = [_sanitize_queue_payload(_decode_object(raw, context=f"{key} payload")) for raw in raw_items]
    return result


async def _queue_contains_target(
    redis: Any,
    queue_name: str,
    *,
    expected_task_id: uuid.UUID,
    expected_video_id: uuid.UUID,
) -> bool:
    snapshot = await _queue_snapshot(redis, queue_name)
    for label in ("queued", "processing"):
        for payload in snapshot[label]:
            if _payload_ids(payload) == (expected_task_id, expected_video_id):
                return True
    return False


async def _target_row(pool: Any, task_id: uuid.UUID) -> Any:
    return await pool.fetchrow(
        """
        SELECT t.id AS task_id,
               t.video_id,
               t.state AS task_state,
               t.queue_name,
               t.account_id,
               t.retries,
               t.max_retries,
               t.retry_not_before,
               t.error_message,
               v.status AS video_status,
               v.info_hash,
               v.local_path,
               v.share_url,
               v.metadata_json
          FROM tasks AS t
          JOIN videos AS v ON v.id = t.video_id
         WHERE t.id = $1
        """,
        task_id,
    )


def _validate_target_row(
    row: Any,
    *,
    expected_task_id: uuid.UUID,
    expected_video_id: uuid.UUID,
    expected_queue: str,
) -> None:
    if row is None:
        raise RuntimeError(f"task {expected_task_id} or its video does not exist")
    if uuid.UUID(str(row["video_id"])) != expected_video_id:
        raise RuntimeError(
            f"task/video relationship mismatch: task {expected_task_id} belongs to {row['video_id']}, "
            f"not {expected_video_id}"
        )
    if str(row["queue_name"]) != expected_queue:
        raise RuntimeError(f"task {expected_task_id} is routed to {row['queue_name']!r}, expected {expected_queue!r}")


def _validate_download_attempt(row: Any) -> None:
    if str(row["task_state"]) not in {
        TaskState.PENDING.value,
        TaskState.DISPATCHED.value,
        TaskState.DOWNLOADING.value,
    }:
        raise RuntimeError(f"download target is in incompatible task state {row['task_state']!r}")
    if row["local_path"]:
        raise RuntimeError("run-one refuses an existing local_path: the download attempt must use qBittorrent")


def _validated_backup(path: Path | None, *, live_identity: str) -> Path:
    if path is None:
        raise RuntimeError("mutating operation requires --database-backup")
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise RuntimeError("database backup must not be a symlink")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"database backup is unavailable: {path}") from exc
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise RuntimeError("database backup must be a non-empty regular file")
    if stat.S_IMODE(resolved.stat().st_mode) & 0o077:
        raise RuntimeError("database backup must be owner-only (mode 0600)")
    sidecar = metadata_path(resolved)
    if sidecar.is_symlink():
        raise RuntimeError("database backup identity sidecar must not be a symlink")
    if sidecar.is_file() and stat.S_IMODE(sidecar.stat().st_mode) & 0o077:
        raise RuntimeError("database backup identity sidecar must be owner-only (mode 0600)")
    require_same_instance(backup=resolved, live_identifier=live_identity, allow_unverified=False)
    with resolved.open("rb") as handle:
        if handle.read(5) != b"PGDMP":
            raise RuntimeError("database backup must be a PostgreSQL custom-format dump")
    return resolved


def _row_evidence(row: Any) -> dict[str, Any]:
    if row is None:
        return {"present": False}
    error = str(row["error_message"] or "")
    prefix = error.partition(":")[0]
    error_class = (prefix if prefix in _SAFE_ERROR_CLASSES else "unclassified") if error else None
    share_url = str(row["share_url"] or "")
    return {
        "present": True,
        "task_id": str(row["task_id"]),
        "video_id": str(row["video_id"]),
        "task_state": str(row["task_state"]),
        "video_status": str(row["video_status"]),
        "queue_name": str(row["queue_name"]),
        "account_id": str(row["account_id"]) if row["account_id"] else None,
        "retries": int(row["retries"] or 0),
        "max_retries": int(row["max_retries"] or 0),
        "retry_not_before": row["retry_not_before"].isoformat() if row["retry_not_before"] else None,
        "error_class": error_class,
        "local_path_present": bool(row["local_path"]),
        "share_kind": "local" if share_url.startswith("pixav-local://") else ("remote" if share_url else None),
    }


def _new_evidence_prefix(evidence_dir: Path, *, command: str, task_id: uuid.UUID) -> Path:
    evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A dot in the timestamp makes Path.with_suffix() discard the task ID.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return evidence_dir / f"{stamp}-{command}-{task_id}"


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    try:
        with create_backup_file(path) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError as exc:
        raise RuntimeError(f"refusing to overwrite evidence file: {path}") from exc
    return path


async def _require_redis_identity(redis: Any, *, operation: str) -> str:
    run_id = await redis_identity(redis)
    if not run_id:
        raise RuntimeError(f"Redis run_id is empty; refusing a guarded {operation}")
    return run_id


async def _assert_same_redis_identity(redis: Any, *, expected: str, operation: str) -> str:
    observed = await redis_identity(redis)
    if observed != expected:
        raise RuntimeError(f"Redis run_id changed while the guarded {operation} was running")
    return observed


async def _write_preflight_evidence(
    *,
    prefix: Path,
    command: str,
    database_identity_value: str,
    redis_identity_value: str,
    database_backup: Path,
    row: Any,
    redis: Any,
    queue_name: str,
) -> Path:
    payload = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "database_identity": database_identity_value,
        "database_backup": database_backup.name,
        "target": _row_evidence(row),
        "redis": {
            "run_id": redis_identity_value,
            "queue_name": queue_name,
            "payloads": await _queue_snapshot(redis, queue_name),
        },
    }
    return _write_json(prefix.with_suffix(".pre.json"), payload)


def _metadata_object(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _metadata_size(raw: Any) -> int | None:
    value = _metadata_object(raw)
    if value is None or not isinstance(value.get("media"), dict):
        return None
    size = value["media"].get("size_bytes")
    try:
        return int(size) if size is not None else None
    except (TypeError, ValueError):
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def _media_evidence(row: Any, *, settings: Settings, require_consistency: bool) -> dict[str, Any] | None:
    local_path = str(row["local_path"] or "")
    if not local_path:
        if require_consistency:
            raise RuntimeError("target video has no local_path")
        return None

    target, missing = safe_cleanup_candidate(settings.remux_dir, local_path)
    if target is None or missing:
        raise RuntimeError("local_path is not a regular, non-symlink file contained by PIXAV_REMUX_DIR")
    file_size = target.stat().st_size
    db_size = _metadata_size(row["metadata_json"])
    media = await probe_media(str(target))
    probe_size = int(media.get("size_bytes") or 0)
    ffprobe_readable = bool(media.get("codec") and media.get("container") and media.get("duration_seconds") is not None)
    evidence = {
        "regular_file": True,
        "contained_in_remux_dir": True,
        "filesystem_size_bytes": file_size,
        "database_media_size_bytes": db_size,
        "ffprobe_size_bytes": probe_size,
        "ffprobe_readable": ffprobe_readable,
        "sha256": await asyncio.to_thread(_sha256_file, target),
        "ffprobe": {
            key: media.get(key)
            for key in ("codec", "container", "duration_seconds", "frame_rate", "height", "resolution", "width")
        },
    }
    evidence["sizes_consistent"] = db_size == file_size == probe_size
    if require_consistency and not evidence["sizes_consistent"]:
        raise RuntimeError(
            f"media size evidence disagrees (database={db_size}, filesystem={file_size}, ffprobe={probe_size})"
        )
    if require_consistency and not ffprobe_readable:
        raise RuntimeError("ffprobe did not return a readable video stream, container, and duration")
    return evidence


def _source_path_absent(row: Any, *, settings: Settings) -> bool | None:
    metadata = _metadata_object(row["metadata_json"])
    torrent = metadata.get("torrent") if metadata else None
    source_name = torrent.get("name") if isinstance(torrent, dict) else None
    if not isinstance(source_name, str) or not source_name:
        return None
    if Path(source_name).name != source_name or source_name in {".", ".."}:
        raise RuntimeError("download metadata contains an unsafe qBittorrent source name")

    root = Path(settings.download_dir).expanduser().resolve(strict=False)
    candidate = root / source_name
    try:
        candidate.lstat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise RuntimeError("could not verify qBittorrent source cleanup") from exc
    return False


async def _qbit_cleanup_evidence(row: Any, *, settings: Settings, timeout_seconds: int = 15) -> dict[str, Any]:
    info_hash = str(row["info_hash"] or "").casefold()
    if len(info_hash) != 40 or any(character not in "0123456789abcdef" for character in info_hash):
        raise RuntimeError("target video has no canonical 40-hex info_hash for qBittorrent cleanup verification")

    client = QBitClient(
        base_url=settings.qbit_url,
        username=settings.qbit_user,
        password=settings.qbit_password,
        download_dir=settings.qbit_download_dir,
        local_download_dir=settings.download_dir,
    )
    try:
        version = await client.health_check()
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while await client.has_torrent(info_hash):
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"qBittorrent still owns target torrent {info_hash} after guarded download")
            await asyncio.sleep(1)
    finally:
        await client.aclose()

    source_absent = _source_path_absent(row, settings=settings)
    if source_absent is False:
        raise RuntimeError("qBittorrent source path still exists after guarded download cleanup")
    return {
        "info_hash": info_hash,
        "qbit_version": version,
        "torrent_absent": True,
        "source_path_absent": source_absent,
    }


async def _status() -> int:
    settings = get_settings()
    pool = await create_pool(settings)
    redis = await create_redis(settings)
    try:
        identity = await database_identity(pool)
        task_rows = await pool.fetch(
            "SELECT state, queue_name, count(*) AS count FROM tasks GROUP BY state, queue_name"
        )
        candidate_table = bool(await pool.fetchval("SELECT to_regclass('public.source_candidates') IS NOT NULL"))
        candidates = int(await pool.fetchval("SELECT count(*) FROM source_candidates")) if candidate_table else None
        usage = shutil.disk_usage(settings.download_dir)
        pause_raw = await redis.get(settings.system_pause_key)
        pause_record: dict[str, Any] | None = None
        if pause_raw is not None:
            try:
                pause_record = _decode_object(pause_raw, context="system pause")
            except RuntimeError:
                pause_record = None
        redis_info = await redis.info("server")

        queue_heads: dict[str, Any] = {}
        for stage, queue_name in (("download", settings.queue_download), ("upload", settings.queue_upload)):
            head = await _effective_queue_head(redis, queue_name)
            if head is None:
                queue_heads[stage] = None
            else:
                payload, source = head
                task_id, video_id = _payload_ids(payload)
                queue_heads[stage] = {
                    "source": source,
                    "task_id": str(task_id) if task_id else None,
                    "video_id": str(video_id) if video_id else None,
                }

        payload = {
            "database_identity": identity,
            "redis_run_id": str(redis_info.get("run_id", "")),
            "disk_free_bytes": usage.free,
            "download_pause_present": await redis.exists(settings.download_pause_key) == 1,
            "download_queue": int(await redis.llen(settings.queue_download)),
            "download_processing": int(await redis.llen(f"{settings.queue_download}:processing")),
            "upload_queue": int(await redis.llen(settings.queue_upload)),
            "upload_processing": int(await redis.llen(f"{settings.queue_upload}:processing")),
            "queue_heads": queue_heads,
            "source_candidates": candidates,
            "system_pause_present": is_paused_value(pause_raw),
            "system_pause_owner": pause_record.get("owner") if pause_record else None,
            "system_pause_token_present": bool(pause_record and pause_record.get("token")),
            "tasks": sorted(
                [
                    {"state": str(row["state"]), "queue_name": str(row["queue_name"]), "count": int(row["count"])}
                    for row in task_rows
                ],
                key=lambda item: (item["state"], item["queue_name"]),
            ),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    finally:
        await redis.aclose()
        await pool.close()


async def _run_one(
    *,
    apply: bool,
    expected_db_identity: str,
    expected_task_id: str = "",
    expected_video_id: str = "",
    database_backup: Path | None = None,
    selected_backup: Path | None = None,
    expected_queued: int | None = None,
    expected_processing: int | None = None,
    evidence_dir: Path = _DEFAULT_EVIDENCE_DIR,
) -> int:
    if not apply:
        raise RuntimeError("run-one is mutating; pass --apply explicitly")
    if not expected_db_identity:
        raise RuntimeError("run-one requires --expect-db-identity")
    task_id = _parse_expected_uuid(expected_task_id, flag="--expect-task-id")
    video_id = _parse_expected_uuid(expected_video_id, flag="--expect-video-id")

    settings = get_settings()
    if settings.media_loader_mode.strip().lower() != "full":
        raise RuntimeError("run-one requires PIXAV_MEDIA_LOADER_MODE=full for a real download attempt")
    pool = await create_pool(settings)
    redis = await create_redis(settings)
    prefix: Path
    try:
        live_identity = await database_identity(pool)
        if live_identity != expected_db_identity:
            raise RuntimeError(f"database identity mismatch: expected {expected_db_identity}, got {live_identity}")
        backup = _validated_backup(database_backup, live_identity=live_identity)
        live_redis_identity = await _require_redis_identity(redis, operation="download")
        if is_paused_value(await redis.get(settings.system_pause_key)):
            raise RuntimeError("global system pause is active; validate VPN gates and explicitly resume it first")

        row = await _target_row(pool, task_id)
        _validate_target_row(
            row,
            expected_task_id=task_id,
            expected_video_id=video_id,
            expected_queue=settings.queue_download,
        )
        _validate_download_attempt(row)
        _assert_queue_target(
            await _effective_queue_head(redis, settings.queue_download),
            queue_name=settings.queue_download,
            expected_task_id=task_id,
            expected_video_id=video_id,
        )
        await validate_selected(
            selected_backup,
            pool,
            redis,
            database_backup=backup,
            expected_queued=expected_queued,
            expected_processing=expected_processing,
            task_id=task_id,
            video_id=video_id,
            queue_name=settings.queue_download,
        )
        prefix = _new_evidence_prefix(evidence_dir, command="download", task_id=task_id)
        await _write_preflight_evidence(
            prefix=prefix,
            command="run-one",
            database_identity_value=live_identity,
            redis_identity_value=live_redis_identity,
            database_backup=backup,
            row=row,
            redis=redis,
            queue_name=settings.queue_download,
        )
        await validate_selected(
            selected_backup,
            pool,
            redis,
            database_backup=backup,
            expected_queued=expected_queued,
            expected_processing=expected_processing,
            task_id=task_id,
            video_id=video_id,
            queue_name=settings.queue_download,
        )
        if is_paused_value(await redis.get(settings.system_pause_key)):
            raise RuntimeError("system pause changed during preflight")

    finally:
        await redis.aclose()
        await pool.close()

    started = time.monotonic()
    await run_loop(
        settings,
        max_tasks=1,
        expected_db_identity=expected_db_identity,
        expected_redis_identity=live_redis_identity,
        expected_task_id=task_id,
        expected_video_id=video_id,
    )
    elapsed = time.monotonic() - started

    pool = await create_pool(settings)
    redis = await create_redis(settings)
    try:
        live_identity = await database_identity(pool)
        if live_identity != expected_db_identity:
            raise RuntimeError("database identity changed while the guarded download was running")
        post_redis_identity = await _assert_same_redis_identity(
            redis,
            expected=live_redis_identity,
            operation="download",
        )
        row = await _target_row(pool, task_id)
        if row is None:
            raise RuntimeError("target task disappeared while the guarded download was running")
        still_queued = await _queue_contains_target(
            redis,
            settings.queue_download,
            expected_task_id=task_id,
            expected_video_id=video_id,
        )
        if still_queued:
            raise RuntimeError("guarded download returned but its payload is still queued or processing")
        media_required = str(row["video_status"]) == VideoStatus.DOWNLOADED.value
        result = {
            "schema_version": 1,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "command": "run-one",
            "database_identity": live_identity,
            "redis_run_id": post_redis_identity,
            "elapsed_seconds": round(elapsed, 3),
            "target": _row_evidence(row),
            "media": await _media_evidence(row, settings=settings, require_consistency=media_required),
            "qbit_cleanup": await _qbit_cleanup_evidence(row, settings=settings),
            "queue_target_absent": True,
        }
        result_path = _write_json(prefix.with_suffix(".result.json"), result)
        result["evidence_file"] = os.fspath(result_path)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        await redis.aclose()
        await pool.close()


async def _upload_one(  # noqa: C901
    *,
    apply: bool,
    mode: str,
    expected_db_identity: str,
    expected_task_id: str = "",
    expected_video_id: str = "",
    database_backup: Path | None = None,
    selected_backup: Path | None = None,
    expected_queued: int | None = None,
    expected_processing: int | None = None,
    evidence_dir: Path = _DEFAULT_EVIDENCE_DIR,
) -> int:
    if not apply:
        raise RuntimeError("upload-one is mutating; pass --apply explicitly")
    if mode != "local":
        raise RuntimeError("upload-one currently permits only --mode local")
    if not expected_db_identity:
        raise RuntimeError("upload-one requires --expect-db-identity")
    task_id = _parse_expected_uuid(expected_task_id, flag="--expect-task-id")
    video_id = _parse_expected_uuid(expected_video_id, flag="--expect-video-id")

    settings = get_settings()
    pool = await create_pool(settings)
    redis = await create_redis(settings)
    prefix: Path
    local_path: str
    try:
        live_identity = await database_identity(pool)
        if live_identity != expected_db_identity:
            raise RuntimeError(f"database identity mismatch: expected {expected_db_identity}, got {live_identity}")
        backup = _validated_backup(database_backup, live_identity=live_identity)
        live_redis_identity = await _require_redis_identity(redis, operation="upload")
        if is_paused_value(await redis.get(settings.system_pause_key)):
            raise RuntimeError("global system pause is active; explicitly resume it before upload-one")

        row = await _target_row(pool, task_id)
        _validate_target_row(
            row,
            expected_task_id=task_id,
            expected_video_id=video_id,
            expected_queue=settings.queue_upload,
        )
        if str(row["task_state"]) not in {
            TaskState.PENDING.value,
            TaskState.DISPATCHED.value,
            TaskState.UPLOADING.value,
        }:
            raise RuntimeError(f"upload target is in incompatible task state {row['task_state']!r}")
        await _media_evidence(row, settings=settings, require_consistency=True)
        local_path = str(row["local_path"])

        head = await _effective_queue_head(redis, settings.queue_upload)
        if head is not None:
            _assert_queue_target(
                head,
                queue_name=settings.queue_upload,
                expected_task_id=task_id,
                expected_video_id=video_id,
            )
            if str(row["task_state"]) == TaskState.PENDING.value:
                raise RuntimeError("upload payload exists while its PostgreSQL task is still pending")

        await validate_selected(
            selected_backup,
            pool,
            redis,
            database_backup=backup,
            expected_queued=expected_queued,
            expected_processing=expected_processing,
            task_id=task_id,
            video_id=video_id,
            queue_name=settings.queue_upload,
        )
        prefix = _new_evidence_prefix(evidence_dir, command="upload-local", task_id=task_id)
        await _write_preflight_evidence(
            prefix=prefix,
            command="upload-one-local",
            database_identity_value=live_identity,
            redis_identity_value=live_redis_identity,
            database_backup=backup,
            row=row,
            redis=redis,
            queue_name=settings.queue_upload,
        )

        await validate_selected(
            selected_backup,
            pool,
            redis,
            database_backup=backup,
            expected_queued=expected_queued,
            expected_processing=expected_processing,
            task_id=task_id,
            video_id=video_id,
            queue_name=settings.queue_upload,
        )
        if is_paused_value(await redis.get(settings.system_pause_key)):
            raise RuntimeError("system pause changed during preflight")

        if head is None:
            if str(row["task_state"]) != TaskState.PENDING.value:
                raise RuntimeError(
                    "upload queue is empty but the task is already claimed; reconcile it before retrying"
                )
            task_repo = TaskRepository(pool)
            claimed = await task_repo.claim_for_dispatch(task_id, next_state=TaskState.DISPATCHED)
            if not claimed:
                raise RuntimeError("exact upload task could not be claimed for one-shot dispatch")
            queue = TaskQueue(redis=redis, queue_name=settings.queue_upload)
            dispatcher = RedisTaskDispatcher(task_repo=task_repo, queues={settings.queue_upload: queue})
            try:
                await dispatcher.dispatch(str(task_id), settings.queue_upload)
            except Exception as exc:
                await task_repo.release_dispatch_claim(task_id, error_message="guarded upload dispatch failed")
                raise RuntimeError("guarded upload dispatch failed; PostgreSQL claim was released") from exc
            _assert_queue_target(
                await _effective_queue_head(redis, settings.queue_upload),
                queue_name=settings.queue_upload,
                expected_task_id=task_id,
                expected_video_id=video_id,
            )
    finally:
        await redis.aclose()
        await pool.close()

    local_settings = settings.model_copy(update={"pixel_injector_mode": "local"})
    started = time.monotonic()
    await run_pixel_worker(
        local_settings,
        max_tasks=1,
        expected_db_identity=expected_db_identity,
        expected_redis_identity=live_redis_identity,
        expected_task_id=task_id,
        expected_video_id=video_id,
    )
    elapsed = time.monotonic() - started

    pool = await create_pool(settings)
    redis = await create_redis(settings)
    try:
        live_identity = await database_identity(pool)
        if live_identity != expected_db_identity:
            raise RuntimeError("database identity changed while the guarded upload was running")
        post_redis_identity = await _assert_same_redis_identity(
            redis,
            expected=live_redis_identity,
            operation="upload",
        )
        row = await _target_row(pool, task_id)
        if row is None:
            raise RuntimeError("target task disappeared while the guarded upload was running")
        queue_absent = not await _queue_contains_target(
            redis,
            settings.queue_upload,
            expected_task_id=task_id,
            expected_video_id=video_id,
        )
        expected_share = f"{settings.pixel_injector_local_share_scheme}{video_id}"
        checks = {
            "task_complete": str(row["task_state"]) == TaskState.COMPLETE.value,
            "video_available": str(row["video_status"]) == VideoStatus.AVAILABLE.value,
            "local_share_exact": str(row["share_url"] or "") == expected_share,
            "local_path_unchanged": str(row["local_path"] or "") == local_path,
            "queue_target_absent": queue_absent,
        }
        media = await _media_evidence(row, settings=settings, require_consistency=True)
        result = {
            "schema_version": 1,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "command": "upload-one-local",
            "database_identity": live_identity,
            "redis_run_id": post_redis_identity,
            "elapsed_seconds": round(elapsed, 3),
            "target": _row_evidence(row),
            "checks": checks,
            "media": media,
        }
        result_path = _write_json(prefix.with_suffix(".result.json"), result)
        result["evidence_file"] = os.fspath(result_path)
        print(json.dumps(result, indent=2, sort_keys=True))
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise RuntimeError(f"guarded local upload postconditions failed: {', '.join(failed)}")
        return 0
    finally:
        await redis.aclose()
        await pool.close()


async def _backup_one(args: argparse.Namespace) -> int:
    task_id = _parse_expected_uuid(args.expect_task_id, flag="--expect-task-id")
    video_id = _parse_expected_uuid(args.expect_video_id, flag="--expect-video-id")
    if args.selected_backup is None or not args.expect_db_identity:
        raise RuntimeError("backup-one requires --selected-backup and --expect-db-identity")
    settings = get_settings()
    pool = await create_pool(settings)
    redis = await create_redis(settings)
    try:
        identity = await database_identity(pool)
        if identity != args.expect_db_identity:
            raise RuntimeError("backup database identity mismatch")
        backup = _validated_backup(args.database_backup, live_identity=identity)
        database = await pool.fetchval("SELECT current_database()")
        if json.loads(metadata_path(backup).read_text()).get("database") != database:
            raise RuntimeError("full backup database name mismatch")
        await _require_redis_identity(redis, operation="backup")
        queue_name = settings.queue_download if args.queue_stage == "download" else settings.queue_upload
        await save_selected(
            args.selected_backup,
            pool,
            redis,
            database_backup=backup,
            task_id=task_id,
            video_id=video_id,
            queue_name=queue_name,
        )
        print(json.dumps({"selected_backup_written": True, "task_id": str(task_id), "video_id": str(video_id)}))
        return 0
    finally:
        await redis.aclose()
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("status", "backup-one", "run-one", "upload-one"), default="status", nargs="?"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--mode", choices=("local",), default="local")
    parser.add_argument("--expect-db-identity", default="")
    parser.add_argument("--expect-task-id", default="")
    parser.add_argument("--expect-video-id", default="")
    parser.add_argument("--database-backup", type=Path, default=None)
    parser.add_argument("--selected-backup", type=Path)
    parser.add_argument("--expect-queued", type=int)
    parser.add_argument("--expect-processing", type=int)
    parser.add_argument("--queue-stage", choices=("download", "upload"), default="download")
    parser.add_argument("--evidence-dir", type=Path, default=_DEFAULT_EVIDENCE_DIR)
    args = parser.parse_args()
    try:
        if args.command == "status":
            code = asyncio.run(_status())
        elif args.command == "backup-one":
            code = asyncio.run(_backup_one(args))
        elif args.command == "run-one":
            code = asyncio.run(
                _run_one(
                    apply=args.apply,
                    expected_db_identity=args.expect_db_identity,
                    expected_task_id=args.expect_task_id,
                    expected_video_id=args.expect_video_id,
                    database_backup=args.database_backup,
                    selected_backup=args.selected_backup,
                    expected_queued=args.expect_queued,
                    expected_processing=args.expect_processing,
                    evidence_dir=args.evidence_dir,
                )
            )
        else:
            code = asyncio.run(
                _upload_one(
                    apply=args.apply,
                    mode=args.mode,
                    expected_db_identity=args.expect_db_identity,
                    expected_task_id=args.expect_task_id,
                    expected_video_id=args.expect_video_id,
                    database_backup=args.database_backup,
                    selected_backup=args.selected_backup,
                    expected_queued=args.expect_queued,
                    expected_processing=args.expect_processing,
                    evidence_dir=args.evidence_dir,
                )
            )
    except (OSError, RuntimeError) as exc:
        parser.exit(2, f"backlog operation refused: {exc}\n")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
