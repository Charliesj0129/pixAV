"""Execute only an operator's exact selection using the normal janitor gates."""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg

from pixav.maxwell_core.cleanup_quiescence import MaintenanceGuard
from pixav.maxwell_core.gc import LocalFileJanitor
from pixav.shared.cleanup_operation import CleanupOperationUnconfirmedError, CleanupSelection
from pixav.shared.cleanup_recovery import CleanupApprovalError, validate_cleanup_recovery


class CleanupOperator:
    def __init__(
        self,
        pool: asyncpg.Pool,
        guard: MaintenanceGuard,
        *,
        staging_root: Path,
        library_root: Path,
        apply_allowed: bool = False,
        allow_operator_exception: bool = False,
    ) -> None:
        self.pool, self.guard = pool, guard
        self.staging_root, self.library_root = staging_root, library_root
        self.apply_allowed = apply_allowed
        self.allow_operator_exception = allow_operator_exception

    async def _janitor(self, selection: CleanupSelection) -> LocalFileJanitor:
        if await self.guard.check() != selection.runtime:
            raise CleanupApprovalError("CLEANUP_RUNTIME_BINDING_MISMATCH")
        identity = await self.pool.fetchrow(
            "SELECT system_identifier::text AS identity,current_database() AS database FROM pg_control_system()"
        )
        target = selection.requests[0].target
        if identity is None or (identity["identity"], identity["database"]) != (
            target.database_identifier,
            target.database_name,
        ):
            raise CleanupApprovalError("CLEANUP_DATABASE_BINDING_MISMATCH")
        return LocalFileJanitor(
            self.pool,
            download_dir=str(self.staging_root),
            library_root=str(self.library_root),
            recovery_runtime=selection.runtime,
            allow_operator_exception=self.allow_operator_exception,
            selection=selection,
            operation_guard=self.guard,
        )

    async def execute(self, selection: CleanupSelection, *, apply: bool = False) -> dict:
        if apply and not self.apply_allowed:
            raise CleanupApprovalError("CLEANUP_APPLY_DISABLED")
        janitor = await self._janitor(selection)
        for request in selection.requests:
            if await janitor._selected_completed(request.target.video_id):
                continue
            if request.runtime != selection.runtime:
                raise CleanupApprovalError("CLEANUP_APPROVAL_RUNTIME_CHANGED")
            await asyncio.to_thread(
                validate_cleanup_recovery,
                request,
                checked_at=await self.pool.fetchval("SELECT clock_timestamp()"),
                allow_operator_exception=self.allow_operator_exception,
            )
        try:
            counts = await janitor.cleanup(apply=apply)
        except Exception as error:
            if not apply:
                raise
            code = str(error) if isinstance(error, CleanupApprovalError) else "CLEANUP_EXECUTION_UNCONFIRMED"
            raise CleanupOperationUnconfirmedError(code) from None
        if apply:
            complete = counts["deleted"] + counts["already_completed"] == selection.expected_count
            result = "APPLIED" if counts["deleted"] else "ALREADY_APPLIED"
        else:
            complete = counts["eligible"] + counts["already_completed"] == selection.expected_count
            result = "ELIGIBLE_FOR_CLEANUP"
        if not complete:
            result = "PARTIAL_OR_REFUSED" if counts["deleted"] else "REFUSED"
        return {"result": result, "expected_count": selection.expected_count, **counts}

    async def recover(self, selection: CleanupSelection, *, apply: bool = False) -> dict:
        # Recovery never unlinks. It remains available with the deployment's
        # deletion switch off, and validates the original committed consent.
        janitor = await self._janitor(selection)
        completed = sum([await janitor._selected_completed(item.target.video_id) for item in selection.requests])
        try:
            counts = await janitor.recover(apply=apply)
        except Exception as error:
            if not apply:
                raise
            code = str(error) if isinstance(error, CleanupApprovalError) else "CLEANUP_RECOVERY_UNCONFIRMED"
            raise CleanupOperationUnconfirmedError(code) from None
        result = "RECOVERY_APPLIED" if apply else "RECOVERY_PREVIEW"
        if completed == selection.expected_count:
            result = "ALREADY_RECONCILED"
        elif completed + sum(counts.values()) == 0:
            result = "NO_MATCHING_COMMITTED_INTENTS"
        return {
            "result": result,
            "expected_count": selection.expected_count,
            "already_completed": completed,
            "eligible_recovery": counts["recovered"],
            "recovered": counts["recovered"] if apply else 0,
            "pending": counts["pending"],
            "rejected": counts["rejected"],
            "without_matching_intent": max(0, selection.expected_count - completed - sum(counts.values())),
        }
