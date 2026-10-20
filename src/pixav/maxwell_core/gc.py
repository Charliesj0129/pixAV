"""Orphan task cleanup and garbage collection."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import uuid
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path

import asyncpg
from pydantic import ValidationError

from pixav.library_projection.repository import RECONCILE_LOCK
from pixav.library_projection.writer import publication_is_complete
from pixav.shared.cleanup_binding import CleanupTargetBinding, capture_cleanup_binding
from pixav.shared.cleanup_recovery import (
    CleanupAuthorizationEvidence,
    CleanupRecoveryAuthorization,
    CleanupRuntimeBinding,
    recovery_authorizes,
)
from pixav.shared.client_playback_evidence import client_matches_current_manifest, parse_client_playback_evidence
from pixav.shared.enums import TaskState
from pixav.shared.media_file import descriptor_facts, file_version, open_media_directory, open_media_file
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


def artifact_facts(path: Path, descriptor: int | None = None) -> tuple[str, int] | None:
    """Recompute the artifact's identity from disk, or None if it cannot be.

    An authorization names the bytes it approved. Reading the file back is what
    turns that row from a claim about a path into a claim about the content
    that is actually there now.
    """
    try:
        if descriptor is None:
            with open_media_file(path) as source:
                return source.facts()
        if file_version(path.lstat()) != file_version(os.fstat(descriptor)):
            return None
        facts = descriptor_facts(descriptor)
        if file_version(path.lstat()) != file_version(os.fstat(descriptor)):
            return None
        return facts
    except (OSError, ValueError):
        return None


async def _artifact_facts_async(path: Path, descriptor: int | None = None) -> tuple[str, int] | None:
    if descriptor is None:
        return await asyncio.to_thread(artifact_facts, path)
    # The thread owns a duplicate. Cancellation may close the caller's fd,
    # but cannot recycle it underneath an unfinished hash or release its lock.
    reader = os.fdopen(os.dup(descriptor), "rb", buffering=0)

    def read_owned() -> tuple[str, int] | None:
        with reader:
            return artifact_facts(path, reader.fileno())

    return await asyncio.to_thread(read_owned)


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
    "single_video_path_reference",
    "cleanup_journal_available",
)


def _intent_data(row) -> tuple[dict, dict] | None:
    try:
        fingerprint, gates = json.loads(row["fingerprint"]), json.loads(row["gates"])
        if (
            not isinstance(fingerprint, dict)
            or not isinstance(gates, dict)
            or not isinstance(gates.get("domain_path"), str)
        ):
            return None
        return fingerprint, gates
    except (TypeError, ValueError, KeyError):
        return None


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
        library_root: str | None = None,
        apply_deletions: bool = False,
        recovery_runtime: CleanupRuntimeBinding | None = None,
        allow_operator_exception: bool = False,
    ) -> None:
        self._pool = pool
        self._download_dir = download_dir
        self._library_root = Path(library_root) if library_root else None
        self._batch_size = max(1, batch_size)
        # Deleting is opt-in for the whole deployment, not a per-call accident.
        self._apply_deletions = apply_deletions
        self._recovery_runtime = recovery_runtime
        self._allow_operator_exception = allow_operator_exception

    @staticmethod
    async def _table_available(conn: asyncpg.Connection, name: str) -> bool:
        """Whether a producer has created the table its evidence would live in."""
        return await conn.fetchval("SELECT to_regclass($1)", f"public.{name}") is not None

    async def _publication_available(self, conn: asyncpg.Connection) -> bool:
        """Whether playback projection facts exist to be checked at all.

        On a schema that predates LibraryProjection no artifact can be shown to
        have a complete projection, so cleanup refuses everything. That refusal
        is correct: an unverified projection is exactly the case BDD-005 forbids.
        """
        return await self._table_available(conn, "library_publications")

    async def _verified_playback(self, conn: asyncpg.Connection, video_id) -> bool:
        """Whether playback was actually verified from the remote copy.

        READY alone says the object was prepared; it does not say anyone ever
        streamed it through the stable URL. Only a recorded end-to-end check of
        a READY object whose remote asset is still DURABLE counts, so a copy
        withdrawn after the check stops counting immediately. On a schema that
        predates PlaybackResolver the table is absent and the gate stays shut,
        naming the missing producer rather than reporting a constant.
        """
        if not await self._table_available(conn, "playable_assets"):
            return False
        row = await conn.fetchrow(
            """SELECT p.*, now() AS checked_at FROM playable_assets p JOIN remote_assets r ON r.id=p.remote_asset_id
                WHERE p.video_id=$1 AND p.state='READY' AND p.playback_verified_at IS NOT NULL
                AND r.state='DURABLE'""",
            video_id,
        )
        if row is None:
            return False
        try:
            evidence = row["playback_evidence"]
            if isinstance(evidence, str):
                evidence = json.loads(evidence)
            client = parse_client_playback_evidence(evidence.get("client_playback"))
            return await client_matches_current_manifest(client, dict(row), conn)
        except (ValidationError, ValueError, TypeError, AttributeError):
            return False

    async def _authorized_target(
        self, conn: asyncpg.Connection, video_id, path: str, descriptor: int | None = None
    ) -> bool:
        return await self._authorization_evidence(conn, video_id, path, descriptor) is not None

    async def _authorization_evidence(
        self, conn: asyncpg.Connection, video_id, path: str, descriptor: int | None = None
    ) -> CleanupAuthorizationEvidence | None:
        """Whether one unexpired authorization names exactly these bytes.

        A row is not consent by itself. The approved digest and byte count are
        compared against facts recomputed from disk here and now, so an
        authorization written before the file changed authorizes nothing, and
        an authorization for a neighbouring path authorizes nothing either.
        """
        if self._recovery_runtime is None or not await self._table_available(conn, "cleanup_authorizations"):
            return None
        row = await conn.fetchrow(
            """SELECT a.id,a.artifact_sha256,a.size_bytes,to_jsonb(a)->'target_binding' AS target_binding,
            to_jsonb(a)->'recovery_authorization' AS recovery_authorization,
            to_jsonb(a)->>'approval_sha256' AS approval_sha256
            FROM cleanup_authorizations a
            JOIN remote_assets r ON r.id = a.remote_asset_id
            JOIN playable_assets p ON p.remote_asset_id=r.id AND p.video_id=a.video_id AND p.state='READY'
            WHERE a.video_id=$1 AND a.path=$2 AND a.expires_at > clock_timestamp()
              AND r.video_id = a.video_id AND r.state='DURABLE'
            ORDER BY a.created_at DESC,a.id DESC LIMIT 1""",
            video_id,
            path,
        )
        if row is None:
            return None
        target, missing = safe_cleanup_candidate(self._download_dir, path)
        if target is None or missing:
            return None
        try:
            # Legacy free-text rows, including schemas before migration 025,
            # remain readable but cannot authorize a newly observed inode.
            binding = row["target_binding"]
            approved = (
                CleanupTargetBinding.model_validate_json(binding)
                if isinstance(binding, str)
                else CleanupTargetBinding.model_validate(binding)
            )
            raw = row["recovery_authorization"]
            recovery = (
                CleanupRecoveryAuthorization.model_validate_json(raw)
                if isinstance(raw, str)
                else CleanupRecoveryAuthorization.model_validate(raw)
            )
            with ExitStack() as resources:
                if descriptor is None:
                    descriptor = resources.enter_context(open_media_file(target)).descriptor
                observed = await _artifact_facts_async(target, descriptor)
                if observed is None or observed != (row["artifact_sha256"], int(row["size_bytes"])):
                    return None
                current = await capture_cleanup_binding(conn, video_id, target, descriptor, observed, domain_path=path)
                checked = await conn.fetchval("SELECT clock_timestamp()")
                if current != approved or not recovery_authorizes(
                    recovery,
                    current,
                    self._recovery_runtime,
                    checked_at=checked,
                    allow_operator_exception=self._allow_operator_exception,
                ):
                    return None
                return CleanupAuthorizationEvidence(
                    version="cleanup-authorization-evidence-v1",
                    authorization_id=row["id"],
                    approval_sha256=row["approval_sha256"],
                    target=current,
                    recovery=recovery,
                )
        except (OSError, ValueError):
            return None

    async def _single_path_reference(self, conn: asyncpg.Connection, video_id, path: str) -> bool:
        """Refuse shared files until group authorization and recovery are available.

        Eligibility belongs to the file, not merely the due video row. Include
        retained workflow/segment references and readers, even when the other
        video has no due cleanup timestamp. Resolve aliases only to identify
        references; deletion itself still refuses symlink components.
        """
        sources = ["SELECT id AS video_id, local_path AS path FROM videos WHERE local_path IS NOT NULL"]
        if await self._table_available(conn, "workflow_artifacts"):
            sources.append(
                "SELECT wt.video_id, a.path FROM workflow_artifacts a "
                "JOIN executions e ON e.id=a.execution_id JOIN workflow_tasks wt ON wt.task_id=e.task_id"
            )
        if await self._table_available(conn, "remote_asset_segments"):
            sources.append(
                "SELECT r.video_id, s.local_path AS path FROM remote_asset_segments s "
                "JOIN remote_assets r ON r.id=s.asset_id WHERE s.local_path IS NOT NULL"
            )
        if await self._table_available(conn, "reader_leases"):
            sources.append("SELECT video_id, path FROM reader_leases WHERE lease_until > now()")
        canonical = os.path.realpath(path)
        for reference in await conn.fetch(" UNION ALL ".join(sources)):
            if reference["path"] and os.path.realpath(reference["path"]) == canonical:
                if reference["video_id"] != video_id:
                    return False
        return True

    async def _evaluate(
        self, conn: asyncpg.Connection, video_id, path: str, descriptor: int | None = None
    ) -> dict[str, bool]:
        """Recompute every gate condition for one artifact, on the DB clock."""
        published = False
        if self._library_root is not None and await self._publication_available(conn):
            publication = await conn.fetchrow(
                """SELECT l.active_version, l.active_digest FROM library_publications l
                JOIN playable_assets p ON p.id=l.playable_asset_id
                JOIN remote_assets r ON r.id=p.remote_asset_id
                WHERE l.video_id=$1 AND p.video_id=l.video_id AND l.state='PUBLISHED'
                  AND p.state='READY' AND r.state='DURABLE'""",
                video_id,
            )
            if publication and publication["active_version"] and publication["active_digest"]:
                published = await asyncio.to_thread(
                    publication_is_complete,
                    self._library_root,
                    video_id,
                    publication["active_version"],
                    publication["active_digest"],
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
            # Two separate producers, so a refusal can say which one is absent:
            # a stream check recorded by PlaybackResolver, and an operator's
            # exact-target authorization that cites that check. Without both,
            # deletion stays closed by contract rather than by a constant.
            "verified_playback_evidence": await self._verified_playback(conn, video_id),
            "exact_target_backup_authorization": await self._authorized_target(conn, video_id, path, descriptor),
            "durable_remote_asset": bool(row["durable"]),
            "complete_publication": published,
            "no_open_execution": not row["open_work"],
            "no_active_reader": not row["reader"],
            "retention_expired": bool(row["due"]),
            "single_video_path_reference": await self._single_path_reference(conn, video_id, path),
            "cleanup_journal_available": await self._table_available(conn, "cleanup_journal"),
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

    @staticmethod
    def _fingerprint(target: Path) -> dict:
        info = target.lstat()
        parent = target.parent.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("cleanup target is not a regular file")
        return {
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
            "parent_device": parent.st_dev,
            "parent_inode": parent.st_ino,
        }

    async def _persist_intent(
        self, conn, video_id, path: str, target: Path, gates: dict, descriptor: int | None = None
    ) -> tuple[uuid.UUID, dict, CleanupAuthorizationEvidence]:
        fingerprint = self._fingerprint(target)
        facts = await _artifact_facts_async(target, descriptor)
        authorized = await self._authorization_evidence(conn, video_id, path, descriptor)
        if facts is None or authorized is None or self._fingerprint(target) != fingerprint:
            raise ValueError("cleanup target changed during intent creation")
        fingerprint["sha256"] = facts[0]
        # This transaction must commit before filesystem mutation. No FK here
        # requests a second connection's lock on the already-locked video.
        async with self._pool.acquire() as journal_conn, journal_conn.transaction():
            existing = await journal_conn.fetchrow(
                "SELECT * FROM cleanup_journal WHERE path=$1 AND state='INTENT'", str(target)
            )
            if existing:
                previous = _intent_data(existing)
                if (
                    previous is None
                    or existing["video_id"] != video_id
                    or previous[0] != fingerprint
                    or previous[1].get("authorization") != authorized.model_dump(mode="json")
                ):
                    raise ValueError("pending cleanup intent names another object")
                return existing["id"], fingerprint, authorized
            intent_id = uuid.uuid4()
            await journal_conn.execute(
                """INSERT INTO cleanup_journal(id,video_id,path,fingerprint,gates)
                VALUES($1,$2,$3,$4::jsonb,$5::jsonb)""",
                intent_id,
                video_id,
                str(target),
                json.dumps(fingerprint),
                json.dumps({**gates, "domain_path": path, "authorization": authorized.model_dump(mode="json")}),
            )
        return intent_id, fingerprint, authorized

    async def _intent_authorization(
        self, conn, row, gates: dict, fingerprint: dict
    ) -> CleanupAuthorizationEvidence | None:
        """Validate the consent committed before unlink, using its historical clock.

        A restarted Redis/daemon does not revoke an already completed unlink.
        Its original authorization and exact DB/file identity must still be in
        the intent; legacy or incomplete intents require manual reconciliation.
        """
        if self._recovery_runtime is None:
            return None
        try:
            authorization = CleanupAuthorizationEvidence.model_validate(gates.get("authorization"))
            target = authorization.target
            approved_path, _ = safe_cleanup_candidate(self._download_dir, gates["domain_path"])
            identity = await conn.fetchrow(
                "SELECT system_identifier::text AS identity,current_database() AS database FROM pg_control_system()"
            )
            if (
                identity is None
                or (identity["identity"], identity["database"]) != (target.database_identifier, target.database_name)
                or target.video_id != row["video_id"]
                or target.path != row["path"]
                or approved_path is None
                or str(approved_path) != target.path
                or target.file.model_dump() != {k: v for k, v in fingerprint.items() if k != "sha256"}
                or target.artifact_sha256 != fingerprint["sha256"]
                or any(gates.get(key) is not True for key in _GATE_CONDITIONS)
                or not recovery_authorizes(
                    authorization.recovery,
                    target,
                    authorization.recovery.runtime,
                    checked_at=row["created_at"],
                    allow_operator_exception=self._allow_operator_exception,
                )
            ):
                return None
            return authorization
        except (ValueError, TypeError, KeyError):
            return None

    async def recover(self, *, apply: bool = False) -> dict[str, int]:
        """Converge missing files with committed intents; never unlink in recovery.

        Existing/replaced files remain pending for normal re-evaluation. The
        default reports only and creates no audit or domain/journal updates.
        """
        counts = {"recovered": 0, "pending": 0, "rejected": 0}
        rows = await self._pool.fetch("SELECT * FROM cleanup_journal WHERE state='INTENT' ORDER BY created_at")
        for row in rows:
            target, missing = safe_cleanup_candidate(self._download_dir, row["path"])
            data = _intent_data(row)
            if data is None or target is None:
                counts["rejected"] += 1
                continue
            fingerprint, gates = data
            if not missing:
                counts["pending"] += 1
                continue
            with ExitStack() as file_handles:
                async with self._pool.acquire() as conn, conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock($1)", RECONCILE_LOCK)
                    authorization = await self._intent_authorization(conn, row, gates, fingerprint)
                    if authorization is None:
                        counts["rejected"] += 1
                        continue
                    current = await conn.fetchval(
                        "SELECT local_path FROM videos WHERE id=$1 FOR UPDATE", row["video_id"]
                    )
                    if current != gates["domain_path"] or not await self._single_path_reference(
                        conn, row["video_id"], current
                    ):
                        counts["rejected"] += 1
                        continue
                    try:
                        directory = file_handles.enter_context(open_media_directory(target, exclusive=True))
                        facts = os.fstat(directory.descriptor)
                        if (facts.st_dev, facts.st_ino) != (fingerprint["parent_device"], fingerprint["parent_inode"]):
                            counts["rejected"] += 1
                            continue
                    except (OSError, ValueError):
                        counts["rejected"] += 1
                        continue
                    # Recheck after waiting for locks; recovery must never clear a
                    # newly restored file's domain path.
                    checked, absent = safe_cleanup_candidate(self._download_dir, row["path"])
                    if checked is None or not absent:
                        counts["rejected"] += 1
                        continue
                    counts["recovered"] += 1
                    if apply:
                        await conn.execute(
                            "UPDATE videos SET local_path=NULL,updated_at=now() WHERE id=$1", row["video_id"]
                        )
                        await self._audit(
                            conn,
                            row["video_id"],
                            current,
                            "missing",
                            True,
                            {
                                "committed_cleanup_intent": str(row["id"]),
                                "filesystem_absent": True,
                                "authorization": authorization.model_dump(mode="json"),
                            },
                        )
                        await conn.execute(
                            "UPDATE cleanup_journal SET state='COMPLETED',completed_at=now() WHERE id=$1", row["id"]
                        )
        return counts

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
            with ExitStack() as file_handles:
                async with self._pool.acquire() as conn, conn.transaction():
                    # Match publication's lock order: advisory lock before row lock.
                    await conn.execute("SELECT pg_advisory_xact_lock($1)", RECONCILE_LOCK)
                    # Protect the video's recorded path for the whole decision.
                    # The inode lock below remains held through the database commit.
                    current_path = await conn.fetchval("SELECT local_path FROM videos WHERE id=$1 FOR UPDATE", video_id)
                    if current_path != path:
                        counts["rejected"] += 1
                        if apply:
                            await self._audit(conn, video_id, path, "rejected", False, {"local_path_unchanged": False})
                        continue
                    locked = None
                    if not missing:
                        try:
                            locked = file_handles.enter_context(open_media_file(target, exclusive=True))
                        except BlockingIOError:
                            counts["rejected"] += 1
                            record_cleanup_rejection("no_active_reader")
                            if apply:
                                await self._audit(conn, video_id, path, "rejected", False, {"no_active_reader": False})
                            continue
                        except (OSError, ValueError):
                            counts["rejected"] += 1
                            if apply:
                                await self._audit(
                                    conn, video_id, path, "rejected", False, {"filesystem_identity": False}
                                )
                            continue
                    gates = await self._evaluate(conn, video_id, path, locked.descriptor if locked else None)
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
                        await self._audit(conn, video_id, path, "missing", False, {**gates, "committed_intent": False})
                        continue
                    try:
                        if locked is None:
                            raise ValueError("cleanup requires an exclusive file lock")
                        intent_id, fingerprint, authorization = await self._persist_intent(
                            conn, video_id, path, target, gates, locked.descriptor
                        )
                        if self._recovery_runtime is None or not recovery_authorizes(
                            authorization.recovery,
                            authorization.target,
                            self._recovery_runtime,
                            checked_at=await conn.fetchval("SELECT clock_timestamp()"),
                            allow_operator_exception=self._allow_operator_exception,
                        ):
                            raise ValueError("cleanup authorization expired before unlink")
                        observed = self._fingerprint(target)
                        if observed != {key: value for key, value in fingerprint.items() if key != "sha256"}:
                            raise ValueError("cleanup target changed after intent commit")
                        locked.unlink()
                    except (OSError, ValueError) as exc:
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
                    await self._audit(
                        conn,
                        video_id,
                        path,
                        "deleted",
                        True,
                        {**gates, "authorization": authorization.model_dump(mode="json")},
                    )
                    await conn.execute(
                        "UPDATE cleanup_journal SET state='COMPLETED',completed_at=now() WHERE id=$1", intent_id
                    )
        return counts
