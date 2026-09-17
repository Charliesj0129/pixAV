"""Private, lossless one-shot snapshots. Restore is restricted to empty test databases."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from scripts.backup_files import create_backup_file
    from scripts.instance_guard import database_identity, metadata_path, redis_identity, require_same_instance
else:
    from backup_files import create_backup_file
    from instance_guard import database_identity, metadata_path, redis_identity, require_same_instance


async def raw_queues(redis: Any, queue_name: str) -> dict[str, list[str]]:
    async with redis.pipeline(transaction=True) as pipe:
        pipe.lrange(queue_name, 0, -1)
        pipe.lrange(f"{queue_name}:processing", 0, -1)
        queued, processing = await pipe.execute()
    return {
        label: [base64.b64encode(raw.encode() if isinstance(raw, str) else raw).decode() for raw in items]
        for label, items in (("queued", queued), ("processing", processing))
    }


async def selected_rows(pool: Any, task_id: uuid.UUID, video_id: uuid.UUID) -> dict[str, Any]:
    # PostgreSQL JSON conversion preserves every column, including dates, JSONB and vectors.
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        rows = {}
        queries = {
            "videos": ("SELECT * FROM videos WHERE id=$1", video_id),
            "tasks": ("SELECT * FROM tasks WHERE id=$1", task_id),
            "source_candidates": ("SELECT * FROM source_candidates WHERE video_id=$1 ORDER BY id", video_id),
            "task_replay_audit": ("SELECT * FROM task_replay_audit WHERE task_id=$1 ORDER BY id", task_id),
            "accounts": ("SELECT * FROM accounts WHERE id=(SELECT account_id FROM tasks WHERE id=$1)", task_id),
            "storage_instances": (
                "SELECT * FROM storage_instances WHERE account_id=(SELECT account_id FROM tasks WHERE id=$1) ORDER BY id",
                task_id,
            ),
        }
        for table, (query, target) in queries.items():
            raw = await conn.fetchval(
                f"SELECT coalesce(jsonb_agg(r), '[]'::jsonb)::text FROM ({query}) r",  # noqa: S608 -- fixed queries above
                target,
            )
            rows[table] = json.loads(raw)
    if len(rows["tasks"]) != 1 or len(rows["videos"]) != 1 or rows["tasks"][0]["video_id"] != str(video_id):
        raise RuntimeError("selected backup requires exactly one matching task/video")
    return rows


async def capture(pool: Any, redis: Any, *, task_id: uuid.UUID, video_id: uuid.UUID, queue_name: str) -> dict[str, Any]:
    return {
        "database_identity": await database_identity(pool),
        "database": await pool.fetchval("SELECT current_database()"),
        "redis_run_id": await redis_identity(redis),
        "task_id": str(task_id),
        "video_id": str(video_id),
        "queue_name": queue_name,
        "rows": await selected_rows(pool, task_id, video_id),
        "queues": await raw_queues(redis, queue_name),
    }


def read_private(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError("selected backup must be a regular non-symlink 0600 file")
    result = json.loads(path.read_text())
    if not isinstance(result, dict) or result.get("schema_version") != 1:
        raise RuntimeError("unsupported selected backup")
    return result


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def save_selected(path: Path, pool: Any, redis: Any, *, database_backup: Path, **target: Any) -> None:
    first = await capture(pool, redis, **target)
    if first != await capture(pool, redis, **target):
        raise RuntimeError("rows or queue changed during backup; stop admission/workers and retry")
    payload = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "full_backup_sha256": file_digest(database_backup),
        "snapshot": first,
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with create_backup_file(path) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


async def validate_selected(
    path: Path | None,
    pool: Any,
    redis: Any,
    *,
    database_backup: Path,
    expected_queued: int | None,
    expected_processing: int | None,
    **target: Any,
) -> None:
    if path is None or expected_queued is None or expected_processing is None:
        raise RuntimeError("one-shot requires --selected-backup, --expect-queued and --expect-processing")
    payload = read_private(path)
    if payload["full_backup_sha256"] != file_digest(database_backup):
        raise RuntimeError("selected backup does not match the full backup")
    snapshot = await capture(pool, redis, **target)
    require_same_instance(backup=database_backup, live_identifier=snapshot["database_identity"], allow_unverified=False)
    if json.loads(metadata_path(database_backup).read_text()).get("database") != snapshot["database"]:
        raise RuntimeError("full backup database name mismatch")
    if snapshot != payload["snapshot"]:
        raise RuntimeError("selected backup is stale or targets a different instance/row/queue")
    if (len(snapshot["queues"]["queued"]), len(snapshot["queues"]["processing"])) != (
        expected_queued,
        expected_processing,
    ):
        raise RuntimeError("exact queue count mismatch")


async def restore_empty_test_database(path: Path, pool: Any, redis: Any, *, queue_name: str) -> None:
    """Restoration drill only: never overwrite existing rows, payloads or production keys.

    Requires a migrated empty test DB on the same verified cluster. A crash between
    DB commit and Redis EXEC is deliberately not retried: inspect/discard the test DB.
    Production recovery must reconcile external side effects before replay.
    """
    from redis.exceptions import WatchError

    payload = read_private(path)
    snapshot = payload["snapshot"]
    name = await pool.fetchval("SELECT current_database()")
    if not name.startswith("pixav_test_") or not queue_name.startswith("pixav:test:"):
        raise RuntimeError("restore drill requires isolated test database and queue")
    if await database_identity(pool) != snapshot["database_identity"]:
        raise RuntimeError("restore cluster identity mismatch")
    if await redis_identity(redis) != snapshot["redis_run_id"]:
        raise RuntimeError("restore Redis identity mismatch")
    keys = (queue_name, f"{queue_name}:processing")
    async with redis.pipeline(transaction=True) as pipe:
        await pipe.watch(*keys)
        if any([await pipe.exists(key) for key in keys]):
            raise RuntimeError("restore destination queues must be empty")
        await _restore_rows(pool, snapshot["rows"])
        pipe.multi()
        for key, label in zip(keys, ("queued", "processing"), strict=True):
            values = [base64.b64decode(raw, validate=True) for raw in snapshot["queues"][label]]
            if values:
                pipe.rpush(key, *values)
        try:
            await pipe.execute()
        except WatchError as exc:
            raise RuntimeError("restore queue changed: DB restored, Redis untouched; inspect test environment") from exc


async def _restore_rows(pool: Any, rows: dict[str, Any]) -> None:
    tables = ("accounts", "storage_instances", "videos", "tasks", "source_candidates", "task_replay_audit")
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("LOCK TABLE " + ", ".join(tables) + " IN ACCESS EXCLUSIVE MODE")
        for table in tables:
            if await conn.fetchval(f"SELECT count(*) FROM {table}"):  # noqa: S608 -- fixed table allowlist
                raise RuntimeError("restore destination tables must be empty")
        for table in tables:
            records = rows[table]
            if table == "accounts":
                records = [dict(row, storage_instance_id=None) for row in records]
            await conn.execute(
                f"INSERT INTO {table} SELECT * FROM jsonb_populate_recordset(NULL::{table}, $1::jsonb)",  # noqa: S608 -- fixed allowlist
                json.dumps(records),
            )
        for row in rows["accounts"]:
            if row["storage_instance_id"]:
                await conn.execute(
                    "UPDATE accounts SET storage_instance_id=$1 WHERE id=$2",
                    uuid.UUID(row["storage_instance_id"]),
                    uuid.UUID(row["id"]),
                )
