"""Register exact cleanup consent; this boundary never unlinks media."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from pathlib import Path
from typing import Protocol

import asyncpg

from pixav.library_projection.repository import RECONCILE_LOCK
from pixav.maxwell_core.gc import LocalFileJanitor, _artifact_facts_async, safe_cleanup_candidate
from pixav.shared.cleanup_binding import CleanupTargetBinding, capture_cleanup_binding
from pixav.shared.cleanup_recovery import (
    CleanupApprovalError,
    CleanupApprovalRequest,
    CleanupRuntimeBinding,
    cleanup_approval_digest,
    recovery_authorizes,
    validate_cleanup_recovery,
)
from pixav.shared.media_file import MediaFile, open_media_file


class CleanupRuntimeGuard(Protocol):
    async def check(self) -> CleanupRuntimeBinding:
        """Recheck daemon/context, Redis identity and owned maintenance pause."""
        ...


class CleanupAuthorizer:
    def __init__(
        self,
        pool: asyncpg.Pool,
        guard: CleanupRuntimeGuard,
        *,
        staging_root: Path,
        library_root: Path,
        allow_operator_exception: bool = False,
    ) -> None:
        self.pool = pool
        self.guard = guard
        self.staging_root = staging_root
        self.library_root = library_root
        self.allow_operator_exception = allow_operator_exception

    async def _locked_target(
        self, conn: asyncpg.Connection, request: CleanupApprovalRequest, files: ExitStack
    ) -> tuple[MediaFile, CleanupTargetBinding]:
        target, missing = safe_cleanup_candidate(str(self.staging_root), request.target.path)
        if target is None or missing:
            raise CleanupApprovalError("CLEANUP_TARGET_UNAVAILABLE")
        path = await conn.fetchval("SELECT local_path FROM videos WHERE id=$1 FOR UPDATE", request.target.video_id)
        if path != request.target.path:
            raise CleanupApprovalError("CLEANUP_TARGET_PATH_CHANGED")
        schema = await conn.fetchval("""SELECT count(*)=3 FROM information_schema.columns
            WHERE table_schema='public' AND table_name='cleanup_authorizations'
            AND column_name IN ('target_binding','recovery_authorization','approval_sha256')""")
        if not schema:
            raise CleanupApprovalError("CLEANUP_RECOVERY_SCHEMA_REQUIRED")
        try:
            locked = files.enter_context(open_media_file(target, exclusive=True))
            facts = await _artifact_facts_async(target, locked.descriptor)
            if facts is None:
                raise CleanupApprovalError("CLEANUP_TARGET_CHANGED")
            current = await capture_cleanup_binding(conn, request.target.video_id, target, locked.descriptor, facts)
        except CleanupApprovalError:
            raise
        except (OSError, ValueError):
            raise CleanupApprovalError("CLEANUP_TARGET_CHANGED_OR_BUSY") from None
        if current != request.target:
            raise CleanupApprovalError("CLEANUP_TARGET_BINDING_MISMATCH")
        return locked, current

    async def authorize(self, request: CleanupApprovalRequest, *, apply: bool = False) -> dict:
        """Dry-run reads only; apply inserts one immutable, idempotent approval."""
        runtime = await self.guard.check()
        if runtime != request.runtime:
            raise CleanupApprovalError("CLEANUP_RUNTIME_BINDING_MISMATCH")
        checked_at = await self.pool.fetchval("SELECT clock_timestamp()")
        recovery = await asyncio.to_thread(
            validate_cleanup_recovery,
            request,
            checked_at=checked_at,
            allow_operator_exception=self.allow_operator_exception,
        )
        digest = cleanup_approval_digest(request)
        with ExitStack() as files:
            async with self.pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", RECONCILE_LOCK)
                locked, current = await self._locked_target(conn, request, files)
                clock = await conn.fetchval("SELECT clock_timestamp()")
                if not recovery_authorizes(
                    recovery,
                    current,
                    runtime,
                    checked_at=clock,
                    allow_operator_exception=self.allow_operator_exception,
                ):
                    raise CleanupApprovalError("CLEANUP_RECOVERY_EXPIRED")
                janitor = LocalFileJanitor(
                    self.pool,
                    download_dir=str(self.staging_root),
                    library_root=str(self.library_root),
                    recovery_runtime=runtime,
                    allow_operator_exception=self.allow_operator_exception,
                )
                gates = await janitor._evaluate(conn, current.video_id, current.path, locked.descriptor)
                # This registration is the producer of this single gate. Every
                # other live gate still comes from the janitor's normal policy.
                gates["exact_target_backup_authorization"] = True
                if not all(gates.values()):
                    raise CleanupApprovalError("CLEANUP_LIVE_GATES_INCOMPLETE")
                previous = await conn.fetchrow(
                    "SELECT id,approval_sha256 FROM cleanup_authorizations WHERE id=$1", request.id
                )
                if previous is not None:
                    if previous["approval_sha256"] != digest:
                        raise CleanupApprovalError("CLEANUP_APPROVAL_ID_CONFLICT")
                if await self.guard.check() != runtime:
                    raise CleanupApprovalError("CLEANUP_RUNTIME_CHANGED")
                locked.assert_current()
                if not recovery_authorizes(
                    recovery,
                    current,
                    runtime,
                    checked_at=await conn.fetchval("SELECT clock_timestamp()"),
                    allow_operator_exception=self.allow_operator_exception,
                ):
                    raise CleanupApprovalError("CLEANUP_RECOVERY_EXPIRED")
                if previous is not None:
                    return {"result": "ALREADY_AUTHORIZED", "expected_count": 1, "authorized_count": 0}
                if not apply:
                    return {
                        "result": "ELIGIBLE_FOR_AUTHORIZATION",
                        "expected_count": 1,
                        "authorized_count": 0,
                        "recovery_mode": recovery.mode,
                    }
                await conn.execute(
                    """INSERT INTO cleanup_authorizations(
                      id,video_id,path,artifact_sha256,size_bytes,remote_asset_id,playback_verified_at,
                      backup_reference,approved_by,reason,expires_at,target_binding,recovery_authorization,approval_sha256
                    ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb,$14)""",
                    request.id,
                    current.video_id,
                    current.path,
                    current.artifact_sha256,
                    current.file.size,
                    current.remote_asset_id,
                    current.playback_verified_at,
                    "receipt-sha256:" + recovery.receipt_sha256,
                    request.approved_by,
                    request.reason,
                    request.expires_at,
                    current.model_dump_json(),
                    recovery.model_dump_json(),
                    digest,
                )
                return {
                    "result": "AUTHORIZED",
                    "expected_count": 1,
                    "authorized_count": 1,
                    "recovery_mode": recovery.mode,
                }
