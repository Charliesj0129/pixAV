#!/usr/bin/env python3
"""Report or remove exact Sehuatang watermark rows and their queued tasks.

The default is read-only. Apply mode requires the count observed during the
dry run and a non-empty full-database dump. It also writes a selected-row JSON
backup before deleting anything. Physical media files are never removed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

from pixav.config import get_settings
from pixav.shared.db import create_pool
from pixav.shared.redis_client import create_redis
from pixav.shared.watermark import KNOWN_WATERMARK, decode_xor_watermark, is_known_watermark

if __package__:
    from scripts.backup_files import create_backup_file
    from scripts.instance_guard import database_identity, redis_identity, require_same_instance
else:  # Support direct execution as well as ``python -m scripts...``.
    from backup_files import create_backup_file
    from instance_guard import database_identity, redis_identity, require_same_instance

_ACTIVE_STATES = {"downloading", "remuxing", "uploading", "verifying"}

# Re-exported so this script keeps a single decoder with the runtime boundary
# guard in pixav.shared.watermark.
__all__ = ["decode_xor_watermark", "is_known_watermark"]


def payload_matches(raw: str | bytes, *, task_ids: set[str], video_ids: set[str]) -> bool:
    """Return whether a Redis queue payload belongs to a selected row."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    return str(payload.get("task_id") or "") in task_ids or str(payload.get("video_id") or "") in video_ids


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date, uuid.UUID)):
        return value.isoformat() if not isinstance(value, uuid.UUID) else str(value)
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "tolist"):
        return value.tolist()
    return str(value)


def _record_dicts(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def has_artifact(row: Mapping[str, Any]) -> bool:
    """Return whether a candidate points at any retained media artifact.

    ``cdn_url`` is absent after migration 009, so this intentionally uses
    ``Mapping.get`` instead of indexing the optional contract-era column.
    """
    return any(row.get(column) for column in ("local_path", "share_url", "cdn_url"))


def write_selected_backup(
    *,
    backup_dir: Path,
    videos: Iterable[Mapping[str, Any]],
    tasks: Iterable[Mapping[str, Any]],
    replay_audit: Iterable[Mapping[str, Any]],
) -> Path:
    """Create a never-overwritten JSON backup for exactly the deleted rows."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = backup_dir / f"watermark-garbage-{stamp}.json"
    payload = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc),
        "classifier": {"decoded_text": KNOWN_WATERMARK},
        "videos": _record_dicts(videos),
        "tasks": _record_dicts(tasks),
        "task_replay_audit": _record_dicts(replay_audit),
    }
    backup_dir.mkdir(parents=True, exist_ok=True)
    with create_backup_file(path) as handle:
        json.dump(payload, handle, default=_json_default, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return path.resolve()


def validate_database_backup(path: Path | None) -> Path:
    """Require a non-empty, regular full-database backup before apply."""
    if path is None:
        raise RuntimeError("--database-backup is required with --apply")
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise RuntimeError(f"database backup is not a non-empty regular file: {resolved}")
    return resolved


async def _matching_queue_counts(
    redis: Any, keys: list[str], task_ids: set[str], video_ids: set[str]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in keys:
        raw_items = cast(list[str | bytes], await redis.lrange(key, 0, -1))
        counts[key] = sum(payload_matches(item, task_ids=task_ids, video_ids=video_ids) for item in raw_items)
    return counts


async def _purge_matching_queue_payloads(
    redis: Any, keys: list[str], task_ids: set[str], video_ids: set[str]
) -> dict[str, int]:
    removed: dict[str, int] = {}
    for key in keys:
        raw_items = cast(list[str | bytes], await redis.lrange(key, 0, -1))
        count = 0
        for raw in raw_items:
            if payload_matches(raw, task_ids=task_ids, video_ids=video_ids):
                count += int(await redis.lrem(key, 0, raw))
        removed[key] = count
    return removed


async def _verify_instance(pool: Any, args: argparse.Namespace, full_backup: Path | None) -> str:
    """Return the live system identifier, refusing on any instance mismatch."""
    async with pool.acquire() as conn:
        live_identifier = await database_identity(conn)
    if args.expect_db_identity and args.expect_db_identity != live_identifier:
        raise RuntimeError(
            f"connected to PostgreSQL system identifier {live_identifier}, "
            f"but --expect-db-identity named {args.expect_db_identity}"
        )
    if full_backup is not None:
        require_same_instance(
            backup=full_backup,
            live_identifier=live_identifier,
            allow_unverified=args.allow_unverified_backup,
        )
    return live_identifier


async def _run(args: argparse.Namespace) -> int:  # noqa: C901
    settings = get_settings()
    if args.apply:
        try:
            full_backup = validate_database_backup(args.database_backup)
        except (OSError, RuntimeError) as exc:
            print(json.dumps({"status": "refused", "reason": str(exc)}, indent=2))
            return 2
    else:
        full_backup = None

    pool = await create_pool(settings)
    redis = await create_redis(settings)

    # Before reading anything, prove which instance answered. `localhost` and
    # the compose service name can be different databases, and a plan built
    # against one and applied to the other deletes rows nobody inspected.
    try:
        live_identifier = await _verify_instance(pool, args, full_backup)
    except RuntimeError as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, indent=2))
        await redis.aclose()
        await pool.close()
        return 2
    queue_keys = [
        settings.queue_download,
        f"{settings.queue_download}:processing",
        settings.queue_upload,
        f"{settings.queue_upload}:processing",
    ]
    try:
        async with pool.acquire() as conn:
            videos = list(await conn.fetch("SELECT * FROM videos WHERE info_hash IS NOT NULL ORDER BY created_at, id"))
            candidates = [row for row in videos if is_known_watermark(str(row["info_hash"]))]
            video_ids = [row["id"] for row in candidates]
            tasks = (
                list(
                    await conn.fetch(
                        "SELECT * FROM tasks WHERE video_id = ANY($1::uuid[]) ORDER BY created_at, id", video_ids
                    )
                )
                if video_ids
                else []
            )
            task_ids = [row["id"] for row in tasks]
            has_audit = bool(await conn.fetchval("SELECT to_regclass('public.task_replay_audit') IS NOT NULL"))
            replay_audit = (
                list(
                    await conn.fetch(
                        "SELECT * FROM task_replay_audit WHERE task_id = ANY($1::uuid[]) ORDER BY created_at, id",
                        task_ids,
                    )
                )
                if task_ids and has_audit
                else []
            )

        task_id_strings = {str(item) for item in task_ids}
        video_id_strings = {str(item) for item in video_ids}
        queued = await _matching_queue_counts(redis, queue_keys, task_id_strings, video_id_strings)
        states = Counter(str(row["state"]) for row in tasks)
        artifacts = [str(row["id"]) for row in candidates if has_artifact(row)]
        processing_matches = sum(count for key, count in queued.items() if key.endswith(":processing"))
        report: dict[str, Any] = {
            "mode": "apply" if args.apply else "dry-run",
            "instance": {
                "db_system_identifier": live_identifier,
                "redis_run_id": await redis_identity(redis),
            },
            "classifier": {"decoded_text": KNOWN_WATERMARK},
            "candidate_videos": len(candidates),
            "candidate_tasks": len(tasks),
            "task_states": dict(sorted(states.items())),
            "queued_payloads": queued,
            "artifact_videos": artifacts,
            "active_tasks": sum(states[state] for state in _ACTIVE_STATES),
            "processing_payloads": processing_matches,
        }
        if not args.apply:
            report["apply_requirements"] = {
                "expected_count": len(candidates),
                "database_backup": "run python -m scripts.backup_postgres first",
                # Echo the identity so an apply can pin the instance this
                # dry-run actually inspected.
                "expect_db_identity": live_identifier,
            }
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0

        if args.expected_count is None or args.expected_count != len(candidates):
            report.update(status="refused", reason="--expected-count does not match the current candidate count")
            print(json.dumps(report, indent=2, sort_keys=True))
            return 2
        if len(candidates) > args.max_delete:
            report.update(status="refused", reason=f"candidate count exceeds --max-delete={args.max_delete}")
            print(json.dumps(report, indent=2, sort_keys=True))
            return 2
        if artifacts and not args.allow_artifacts:
            report.update(status="refused", reason="candidate videos have local/share/CDN artifacts")
            print(json.dumps(report, indent=2, sort_keys=True))
            return 2
        if (report["active_tasks"] or processing_matches) and not args.allow_active:
            report.update(status="refused", reason="candidate tasks are active or present in a processing list")
            print(json.dumps(report, indent=2, sort_keys=True))
            return 2

        pause_payload = {
            "reason": "operator watermark cleanup in progress; resume manually after verification",
            "paused_at": datetime.now(timezone.utc).isoformat(),
            "source": "cleanup_watermark_garbage",
        }
        await redis.set(settings.download_pause_key, json.dumps(pause_payload, sort_keys=True))

        async with pool.acquire() as conn, conn.transaction():
            locked = list(
                await conn.fetch(
                    "SELECT * FROM videos WHERE id = ANY($1::uuid[]) ORDER BY created_at, id FOR UPDATE",
                    video_ids,
                )
            )
            if len(locked) != len(candidates) or any(not is_known_watermark(str(row["info_hash"])) for row in locked):
                raise RuntimeError("candidate set changed while acquiring the deletion lock")
            selected_backup = write_selected_backup(
                backup_dir=args.backup_dir,
                videos=locked,
                tasks=tasks,
                replay_audit=replay_audit,
            )
            deleted_rows = await conn.fetch("DELETE FROM videos WHERE id = ANY($1::uuid[]) RETURNING id", video_ids)
            if len(deleted_rows) != len(candidates):
                raise RuntimeError("deleted video count did not match the locked candidate count")

        removed = await _purge_matching_queue_payloads(redis, queue_keys, task_id_strings, video_id_strings)
        report.update(
            status="applied",
            database_backup=str(full_backup),
            selected_rows_backup=str(selected_backup),
            deleted_videos=len(candidates),
            cascade_deleted_tasks=len(tasks),
            removed_queue_payloads=removed,
            download_pause="left latched; verify, then run manage_download_pause.py resume",
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    finally:
        await redis.aclose()
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--database-backup", type=Path)
    parser.add_argument("--backup-dir", type=Path, default=Path("backups"))
    parser.add_argument("--max-delete", type=int, default=500)
    parser.add_argument("--allow-active", action="store_true")
    parser.add_argument("--allow-artifacts", action="store_true")
    parser.add_argument(
        "--expect-db-identity",
        default="",
        help="refuse unless the connected cluster reports this pg_control_system() identifier",
    )
    parser.add_argument(
        "--allow-unverified-backup",
        action="store_true",
        help="accept a full backup taken before .meta.json sidecars existed (a mismatch is never accepted)",
    )
    args = parser.parse_args()
    try:
        code = asyncio.run(_run(args))
    except Exception as exc:
        parser.exit(1, f"cleanup failed: {exc}\n")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
