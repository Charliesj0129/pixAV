"""Orphan task cleanup and garbage collection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import uuid
from datetime import timedelta
from pathlib import Path

import asyncpg

from pixav.shared.enums import TaskState
from pixav.shared.metrics import record_cleanup_rejection

logger = logging.getLogger(__name__)

# Tasks stuck in transient states for longer than this are considered orphans
_DEFAULT_ORPHAN_AGE = timedelta(hours=2)

# Transient states that should not persist indefinitely
_TRANSIENT_STATES = (
    TaskState.DOWNLOADING,
    TaskState.REMUXING,
    TaskState.UPLOADING,
    TaskState.VERIFYING,
)


class OrphanTaskCleaner:
    """Detect and clean up orphaned tasks.

    A task is considered orphaned if it has been in a transient state
    (downloading, remuxing, uploading, verifying) for longer than
    ``max_age`` without progressing.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        max_age: timedelta = _DEFAULT_ORPHAN_AGE,
    ) -> None:
        self._pool = pool
        self._max_age = max_age

    async def cleanup(self) -> int:
        """Find orphaned tasks and mark them as FAILED.

        Returns:
            Number of orphaned tasks that were cleaned up.
        """
        transient_values = [s.value for s in _TRANSIENT_STATES]

        result = await self._pool.execute(
            """
            UPDATE tasks
               SET state = $1,
                   error_message = 'orphan cleanup: stuck in transient state',
                   updated_at = now()
             WHERE state = ANY($2::text[])
               AND queue_name <> 'pixav:media-managed'
               AND updated_at < now() - $3::interval
            """,
            TaskState.FAILED.value,
            transient_values,
            self._max_age,
        )

        # result is like "UPDATE N"
        count = _parse_update_count(result)
        if count > 0:
            logger.warning("cleaned up %d orphaned tasks", count)
        else:
            logger.debug("no orphaned tasks found")
        return count

    async def cleanup_expired_videos(self) -> int:
        """Mark videos with expired share URLs as ``expired``.

        Returns:
            Number of videos marked as expired.
        """
        result = await self._pool.execute("""
            UPDATE videos
               SET status = 'expired', updated_at = now()
             WHERE status = 'available'
               AND share_url IS NOT NULL
               AND updated_at < now() - interval '30 days'
            """)
        count = _parse_update_count(result)
        if count > 0:
            logger.info("marked %d videos as expired", count)
        return count


def _parse_update_count(result: str) -> int:
    """Parse PostgreSQL UPDATE command result to extract row count."""
    # asyncpg returns strings like "UPDATE 5"
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


def safe_cleanup_candidate(download_dir: str, local_path: str) -> tuple[Path | None, bool]:
    """Resolve a cleanup target without following symlinks.

    Returns ``(path, missing)`` for safe ordinary files/missing paths and
    ``(None, False)`` for paths outside the root, symlinks, or non-files.
    """
    # Normalize both absolute and relative inputs before checking containment.
    # ``Path.relative_to`` does not collapse ``..`` in an already-absolute path.
    configured_root = Path(os.path.abspath(download_dir))
    candidate = Path(os.path.abspath(local_path))
    try:
        relative = candidate.relative_to(configured_root)
    except ValueError:
        return None, False

    # Resolving the configured root itself is intentional: deployments may
    # mount it through a stable symlink. Candidate components are still walked
    # one by one and are never resolved through symlinks.
    root = configured_root.resolve(strict=False)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return None, False

    try:
        mode = current.lstat().st_mode
    except FileNotFoundError:
        return current, True
    except OSError:
        return None, False
    if not stat.S_ISREG(mode):
        return None, False
    return current, False


def artifact_facts(path: Path) -> tuple[str, int] | None:
    """Recompute the artifact's identity from disk, or None if it cannot be.

    An authorization names the bytes it approved. Reading the file back is what
    turns that row from a claim about a path into a claim about the content
    that is actually there now.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb", buffering=0) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest(), info.st_size
    except OSError:
        return None


# Every one of these must hold before a local artifact may be removed. They are
# named so a refusal says which guarantee was missing, not just "not eligible".
_GATE_CONDITIONS = (
    "verified_playback_evidence",
    "exact_target_backup_authorization",
    "durable_remote_asset",
    "complete_publication",
    "no_open_execution",
    "no_active_reader",
    "retention_expired",
)


class LocalFileJanitor:
    """Remove due local files only once every durability guarantee holds.

    Dry-run is the default: an evaluation that changes nothing is how an
    operator inspects what would be deleted. ``apply`` removes exactly the
    artifacts this same evaluation approved, re-checking each one under a row
    lock so a reader or execution appearing mid-batch still blocks the unlink.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        download_dir: str,
        batch_size: int = 100,
        apply_deletions: bool = False,
    ) -> None:
        self._pool = pool
        self._download_dir = download_dir
        self._batch_size = max(1, batch_size)
        # Deleting is opt-in for the whole deployment, not a per-call accident.
        self._apply_deletions = apply_deletions

    @staticmethod
    async def _table_available(conn: asyncpg.Connection, name: str) -> bool:
        """Whether a producer has created the table its evidence would live in."""
        return await conn.fetchval("SELECT to_regclass($1)", f"public.{name}") is not None

    async def _publication_available(self, conn: asyncpg.Connection) -> bool:
        """Whether playback projection facts exist to be checked at all.

        While LibraryProjection has not landed, no artifact can be shown to have
        a complete projection, so cleanup refuses everything. That refusal is
        correct: an unverified projection is exactly the case BDD-005 forbids.
        """
        return await self._table_available(conn, "library_publications")

    async def _verified_playback(self, conn: asyncpg.Connection, video_id) -> bool:
        """Whether playback was actually verified from the remote copy.

        PlaybackResolver owns ``playable_assets``; it has not landed, so this
        table does not exist and the condition is False for every artifact in
        production. That is the blocking dependency, named -- the point of
        naming it is that a refusal can say which producer is missing instead
        of reporting a constant nobody can act on.
        """
        if not await self._table_available(conn, "playable_assets"):
            return False
        return bool(
            await conn.fetchval(
                "SELECT EXISTS(SELECT FROM playable_assets WHERE video_id=$1 AND state='READY')", video_id
            )
        )

    async def _authorized_target(self, conn: asyncpg.Connection, video_id, path: str) -> bool:
        """Whether one unexpired authorization names exactly these bytes.

        A row is not consent by itself. The approved digest and byte count are
        compared against facts recomputed from disk here and now, so an
        authorization written before the file changed authorizes nothing, and
        an authorization for a neighbouring path authorizes nothing either.
        """
        if not await self._table_available(conn, "cleanup_authorizations"):
            return False
        # playback_verified_at and the backup reference are NOT NULL in the
        # schema, so a row that exists already carries both.
        row = await conn.fetchrow(
            """SELECT a.artifact_sha256, a.size_bytes FROM cleanup_authorizations a
            JOIN remote_assets r ON r.id = a.remote_asset_id
            WHERE a.video_id=$1 AND a.path=$2 AND a.expires_at > now()
              AND r.video_id = a.video_id AND r.state='DURABLE'
            ORDER BY a.created_at DESC LIMIT 1""",
            video_id,
            path,
        )
        if row is None:
            return False
        target, missing = safe_cleanup_candidate(self._download_dir, path)
        if target is None or missing:
            return False
        # Only reached once an authorization exists, so the cost of reading the
        # artifact back is paid for a decision that is actually about to be made.
        observed = await asyncio.to_thread(artifact_facts, target)
        return observed == (row["artifact_sha256"], int(row["size_bytes"]))

    async def _evaluate(self, conn: asyncpg.Connection, video_id, path: str) -> dict[str, bool]:
        """Recompute every gate condition for one artifact, on the DB clock."""
        published = False
        if await self._publication_available(conn):
            published = bool(
                await conn.fetchval(
                    """SELECT EXISTS(SELECT FROM library_publications
                    WHERE video_id=$1 AND state='PUBLISHED')""",
                    video_id,
                )
            )
        row = await conn.fetchrow(
            """SELECT
                EXISTS(SELECT FROM remote_assets WHERE video_id=$1 AND state='DURABLE') AS durable,
                EXISTS(SELECT FROM tasks WHERE video_id=$1 AND state NOT IN ('complete','failed'))
                    OR EXISTS(SELECT FROM executions e JOIN workflow_tasks wt ON wt.task_id=e.task_id
                        WHERE wt.video_id=$1 AND e.state NOT IN ('SUCCEEDED','FAILED','CANCELLED')) AS open_work,
                EXISTS(SELECT FROM reader_leases WHERE video_id=$1 AND lease_until > now()) AS reader,
                EXISTS(SELECT FROM videos WHERE id=$1 AND local_path=$2
                    AND local_cleanup_after IS NOT NULL AND local_cleanup_after <= now()) AS due""",
            video_id,
            path,
        )
        observed = {
            # Two separate producers, so a refusal can say which one is absent.
            # Neither is satisfiable today: Component D has not landed, so there
            # is no verified playback to point at and no honest authorization
            # anyone could write. Deletion stays closed, by contract rather than
            # by a constant.
            "verified_playback_evidence": await self._verified_playback(conn, video_id),
            "exact_target_backup_authorization": await self._authorized_target(conn, video_id, path),
            "durable_remote_asset": bool(row["durable"]),
            "complete_publication": published,
            "no_open_execution": not row["open_work"],
            "no_active_reader": not row["reader"],
            "retention_expired": bool(row["due"]),
        }
        # Keyed on the declared list so a new guarantee cannot be added to the
        # contract without this evaluation being forced to answer for it.
        return {name: observed[name] for name in _GATE_CONDITIONS}

    async def _audit(self, conn, video_id, path: str, decision: str, applied: bool, reasons: dict) -> None:
        await conn.execute(
            """INSERT INTO cleanup_audit(id,video_id,path,decision,applied,reasons)
            VALUES($1,$2,$3,$4,$5,$6::jsonb)""",
            uuid.uuid4(),
            video_id,
            path,
            decision,
            applied,
            json.dumps(reasons),
        )

    async def cleanup(  # noqa: C901 -- explicit refusal/audit paths
        self, *, apply: bool | None = None
    ) -> dict[str, int]:
        apply = self._apply_deletions if apply is None else apply
        counts = {"deleted": 0, "missing": 0, "failed": 0, "unsafe": 0, "rejected": 0, "eligible": 0}
        rows = await self._pool.fetch(
            """SELECT id, local_path FROM videos
            WHERE local_path IS NOT NULL AND local_cleanup_after IS NOT NULL AND local_cleanup_after <= now()
            ORDER BY local_cleanup_after LIMIT $1""",
            self._batch_size,
        )
        for row in rows:
            video_id, path = row["id"], row["local_path"]
            target, missing = safe_cleanup_candidate(self._download_dir, path)
            if target is None:
                counts["unsafe"] += 1
                if apply:
                    async with self._pool.acquire() as conn:
                        await self._audit(conn, video_id, path, "unsafe", False, {"path_containment": False})
                continue
            async with self._pool.acquire() as conn, conn.transaction():
                # Hold the row for the whole decision so nothing can start
                # reading, or open an execution, between the check and the unlink.
                await conn.execute("SELECT id FROM videos WHERE id=$1 FOR UPDATE", video_id)
                gates = await self._evaluate(conn, video_id, path)
                if not all(gates.values()):
                    counts["rejected"] += 1
                    for condition, satisfied in gates.items():
                        if not satisfied:
                            record_cleanup_rejection(condition)
                    if apply:
                        await self._audit(conn, video_id, path, "rejected", False, gates)
                    continue
                counts["eligible"] += 1
                if not apply:
                    # Dry-run never writes audit rows, domain state or files.
                    continue
                if missing or not target.exists():
                    counts["missing"] += 1
                    await conn.execute(
                        "UPDATE videos SET local_path=NULL,updated_at=now() WHERE id=$1 AND local_path=$2",
                        video_id,
                        path,
                    )
                    await self._audit(conn, video_id, path, "missing", True, gates)
                    continue
                try:
                    target.unlink()
                except OSError as exc:
                    counts["failed"] += 1
                    logger.error("cleanup could not remove an approved artifact: %s", type(exc).__name__)
                    await self._audit(conn, video_id, path, "failed", True, {**gates, "error": type(exc).__name__})
                    continue
                counts["deleted"] += 1
                await conn.execute(
                    "UPDATE videos SET local_path=NULL,updated_at=now() WHERE id=$1 AND local_path=$2",
                    video_id,
                    path,
                )
                await self._audit(conn, video_id, path, "deleted", True, gates)
        return counts
