"""Managed storage activity worker: uploads and cold read-backs.

It reports what happened and nothing else. It cannot pick an account, schedule a
retry, decide that an upload succeeded, or advance any execution state — those
belong to the execution authority. Every external effect is journalled through
``journal_segment`` before it is attempted, so a crash leaves an intent the
authority can reconcile rather than an unexplained remote object.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Protocol
from uuid import UUID, uuid4

import asyncpg
from pydantic import ValidationError

from pixav.pixel_injector.canary import CanaryBlockedError
from pixav.pixel_injector.maestro_parts import UploadReviewRequiredError
from pixav.pixel_injector.photos_auth import AccountSessionRequiredError
from pixav.shared.exceptions import ReadbackUnavailableError, RedroidError, UploadError, VerificationError
from pixav.shared.models import Account
from pixav.shared.queue import TaskQueue
from pixav.shared.remote_assets import expectation, segment_from_row
from pixav.shared.storage_models import (
    IntegrityError,
    RemoteAssetSegment,
    VerificationPolicy,
    current_policy,
    policy_for,
)
from pixav.shared.upload_outcome_review import (
    ExistingItemShareReview,
    parse_upload_review,
    recovery_digest,
    validate_review_cold_binding,
    validate_share_recovery,
)
from pixav.shared.workflow import ActivityRequest, ActivityResult

logger = logging.getLogger(__name__)


class QuotaExhaustedError(RuntimeError):
    """The provider refused the transfer for capacity reasons, not a defect."""


class JournalFn(Protocol):
    async def __call__(self, state: str, recovery: dict) -> None: ...


class SegmentUploader(Protocol):
    """Places one segment with the provider using the configured environment."""

    async def upload(
        self,
        segment: RemoteAssetSegment,
        account: Account,
        journal: JournalFn,
    ) -> tuple[str, dict]:
        """Return ``(share_url, backup_evidence)`` once the provider accepted it."""
        ...

    async def share_existing(
        self,
        segment: RemoteAssetSegment,
        account: Account,
        journal: JournalFn,
    ) -> tuple[str, dict]:
        """Inspect/share only; no staging, push, publication or login."""
        ...

    async def release(self, segment: RemoteAssetSegment) -> None:
        """Drop whatever local presentation the upload required. Optional."""
        ...


class SegmentReadback(Protocol):
    """Re-acquires one segment from the provider in a fresh, cold session."""

    async def read_back(self, segment: RemoteAssetSegment) -> dict:
        """Return an integrity receipt describing how the bytes were obtained."""
        ...


class StorageActivityWorker:
    """Claim, act, report. The authority owns everything else."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        uploader: SegmentUploader,
        readback: SegmentReadback,
        *,
        upload_timeout_seconds: int = 21600,
        verify_timeout_seconds: int = 21600,
    ) -> None:
        self.pool = pool
        self.uploader = uploader
        self.readback = readback
        self.upload_timeout_seconds = upload_timeout_seconds
        self.verify_timeout_seconds = verify_timeout_seconds
        # Share locations and backup evidence are judged the same way under
        # every version; anything version-bound is looked up per asset.
        self.policy = current_policy()

    async def _segment(self, asset_id: UUID, segment_index: int) -> RemoteAssetSegment:
        row = await self.pool.fetchrow(
            "SELECT * FROM remote_asset_segments WHERE asset_id=$1 AND segment_index=$2",
            asset_id,
            segment_index,
        )
        if row is None:
            raise UploadError("the authority addressed a segment that does not exist")
        return segment_from_row(row)

    async def _asset_policy(self, asset_id: UUID) -> tuple[VerificationPolicy, dict]:
        """The frozen rules this asset's evidence belongs to, and what it stands for.

        An asset created under an older version keeps being judged by it, so
        the worker reads the version from the asset rather than assuming the
        one it happens to have been built with.
        """
        row = await self.pool.fetchrow("SELECT policy_version,expected FROM remote_assets WHERE id=$1", asset_id)
        if row is None:
            raise VerificationError("the authority addressed an asset that does not exist")
        return policy_for(row["policy_version"]), expectation(row)

    async def _account(self, account_id: UUID) -> Account:
        row = await self.pool.fetchrow("SELECT * FROM accounts WHERE id=$1", account_id)
        if row is None:
            raise UploadError("leased account is missing")
        return Account.model_validate(dict(row))

    def _journal(self, request: ActivityRequest, token: UUID) -> JournalFn:
        async def journal(state: str, recovery: dict) -> None:
            """Record the intent before the effect, under this exact lease."""
            accepted = await self.pool.fetchval(
                "SELECT journal_segment($1::jsonb)",
                _json(
                    {
                        "asset_id": str(request.asset_id),
                        "segment_index": request.segment_index,
                        "state": state,
                        "recovery": recovery,
                        "account_id": str(request.account_id) if request.account_id else None,
                        "attempt_id": str(request.attempt_id),
                        "token": str(token),
                    }
                ),
            )
            if not accepted:
                # The lease moved on, or the segment is already confirmed. Either
                # way this worker must not create a further external effect.
                raise RedroidError("storage intent rejected; lease is no longer current")

        return journal

    async def execute(self, request: ActivityRequest, token: UUID) -> ActivityResult:
        fields = request.model_dump(exclude={"stage", "identity", "input_path", "share_url"})
        try:
            if request.asset_id is None or request.segment_index is None:
                raise UploadError("storage activity must address a segment")
            segment = await self._segment(request.asset_id, request.segment_index)
            if request.stage == "upload":
                return await self._upload(request, segment, token, fields)
            return await self._verify(request.asset_id, segment, fields)
        except (CanaryBlockedError, AccountSessionRequiredError) as exc:
            # Includes login challenges. The adapter requires UI/runtime review;
            # infrastructure retries cannot resolve an unknown remote effect.
            return ActivityResult(**fields, outcome="user_action", error_code=_review_error_code(exc))
        except QuotaExhaustedError:
            return ActivityResult(**fields, outcome="quota_exhausted", error_code="QUOTA_EXHAUSTED")
        except IntegrityError:
            return ActivityResult(**fields, outcome="verification_failed", error_code="INTEGRITY_MISMATCH")
        except (ReadbackUnavailableError, asyncio.TimeoutError) as exc:
            return self._unavailable(request, fields, exc)
        except VerificationError:
            return ActivityResult(**fields, outcome="verification_failed", error_code="READBACK_FAILED")
        except (RedroidError, UploadError):
            # The remote effect may or may not have landed. Saying "failed" here
            # would authorize a blind resend, so the authority reconciles first.
            return ActivityResult(**fields, outcome="unknown_effect", error_code="REMOTE_EFFECT_UNKNOWN")
        except Exception:
            logger.error("storage activity dependency failed")
            return ActivityResult(**fields, outcome="infrastructure", error_code="DEPENDENCY_FAILURE")

    @staticmethod
    def _unavailable(request, fields: dict, error: Exception) -> ActivityResult:
        if request.stage != "verify":
            return ActivityResult(**fields, outcome="unknown_effect", error_code="REMOTE_EFFECT_TIMEOUT")
        kind = error.error_type if isinstance(error, ReadbackUnavailableError) else "TimeoutError"
        return ActivityResult(
            **fields, outcome="infrastructure", error_code="DEPENDENCY_FAILURE", evidence={"readback_dependency": kind}
        )

    async def _upload(self, request, segment, token, fields) -> ActivityResult:
        if request.account_id is None:
            raise UploadError("upload activity requires a leased account")
        account = await self._account(request.account_id)
        journal = self._journal(request, token)
        await journal("upload_intent", dict(segment.recovery))
        if "upload_outcome_review" in segment.recovery:
            return await self._review_existing_upload(request, segment, fields, account, journal)
        share_url, evidence = await asyncio.wait_for(
            self.uploader.upload(segment, account, journal), timeout=self.upload_timeout_seconds
        )
        self.policy.validate_share_location(share_url)
        self.policy.validate_backup_evidence(evidence)
        return ActivityResult(**fields, outcome="success", share_url=share_url, evidence=evidence)

    async def _review_existing_upload(self, request, segment, fields, account, journal) -> ActivityResult:
        stored = segment.recovery["upload_outcome_review"]
        review = parse_upload_review(stored["evidence"])
        if (
            stored.get("status") != "ACCEPTED"
            or review.operation_id != request.operation_id
            or review.execution_id != request.execution_id
            or review.account_id != request.account_id
            or review.asset_id != segment.asset_id
            or review.segment_index != segment.segment_index
            or review.segment_sha256 != segment.sha256
            or request.generation <= review.generation
        ):
            raise CanaryBlockedError("existing upload review binding changed")
        if isinstance(review, ExistingItemShareReview):
            try:
                validate_share_recovery(stored, segment.recovery)
            except ValueError as exc:
                raise CanaryBlockedError("existing share recovery binding changed") from exc
            if review.filename != segment.filename:
                raise CanaryBlockedError("existing item filename changed")
            share_url, backup_evidence = await asyncio.wait_for(
                self.uploader.share_existing(segment, account, journal), timeout=self.upload_timeout_seconds
            )
        else:
            if review.recovery_sha256 != recovery_digest(segment.recovery):
                raise CanaryBlockedError("existing upload recovery binding changed")
            share_url, backup_evidence = review.share_url, review.backup_evidence
        # Every path must acquire fresh provider-only bytes before reporting
        # success. A sharing link never authorizes push or publication.
        candidate = segment.model_copy(update={"share_url": share_url})
        policy, expected = await self._asset_policy(segment.asset_id)
        policy.validate_share_location(share_url)
        policy.validate_backup_evidence(backup_evidence)
        receipt = await asyncio.wait_for(self.readback.read_back(candidate), timeout=self.verify_timeout_seconds)
        validate_review_cold_binding(candidate, share_url, receipt)
        policy.validate_segment_readback(candidate, receipt)
        policy.validate_segment_media(candidate, expected, receipt)
        evidence = {
            **backup_evidence,
            "upload_outcome_review": {
                "version": review.version,
                "artifact_sha256": review.artifact_sha256,
                "provider_completed_after": review.provider_completed_after.isoformat(),
                "provider_completed_before": review.provider_completed_before.isoformat(),
                "confirmed_at": stored["confirmed_at"],
                "cold_readback": receipt,
            },
        }
        return ActivityResult(**fields, outcome="success", share_url=share_url, evidence=evidence)

    async def _verify(self, asset_id: UUID, segment, fields) -> ActivityResult:
        if not segment.share_url:
            raise VerificationError("nothing to read back: the segment has no share location")
        policy, expected = await self._asset_policy(asset_id)
        evidence = await asyncio.wait_for(self.readback.read_back(segment), timeout=self.verify_timeout_seconds)
        # Validated here as well as in the authority: a receipt that cannot pass
        # must never travel as a success, even briefly. Reporting it and letting
        # the authority reject it would turn an integrity failure into a report
        # the authority cannot classify.
        policy.validate_segment_readback(segment, evidence)
        policy.validate_segment_media(segment, expected, evidence)
        await self._release(segment)
        return ActivityResult(**fields, outcome="success", evidence=evidence)

    async def _release(self, segment: RemoteAssetSegment) -> None:
        """The provider now holds these bytes; the upload presentation can go.

        Failing to tidy up is not a reason to report a verified read-back as
        anything other than success, so this never raises into the result.
        """
        release = getattr(self.uploader, "release", None)
        if release is None:
            return
        try:
            await release(segment)
        except Exception:  # noqa: BLE001 - tidying must not mask a good result
            logger.warning("could not release the staged segment presentation", exc_info=True)

    async def run_one(self, queue: TaskQueue) -> bool:
        claimed = await queue.pop_claim(timeout=1)
        if claimed is None:
            return False
        payload, receipt = claimed
        try:
            request = ActivityRequest.model_validate(payload)
        except ValidationError:
            logger.warning("invalid storage activity envelope")
            await queue.ack(receipt)
            return True
        if request.stage not in ("upload", "verify"):
            logger.warning("storage transport carried a non-storage stage")
            await queue.ack(receipt)
            return True
        if not await self._matches_intent(request):
            await queue.ack(receipt)
            return True
        token = uuid4()
        if not await self.pool.fetchval("SELECT claim_activity($1,$2)", request.attempt_id, token):
            await queue.ack(receipt)
            return True
        running = asyncio.create_task(self.execute(request, token))
        heartbeat = asyncio.create_task(self._heartbeat(request, token, running))
        try:
            result = await running
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
            """SELECT e.task_id,e.owner,e.generation,e.stage,a.operation_id,o.identity
            FROM executions e JOIN activity_attempts a ON a.execution_id=e.id
            JOIN operation_intents o ON o.id=a.operation_id WHERE e.id=$1 AND a.id=$2
            AND a.generation=e.generation AND e.state='RUNNING' AND e.lease_until > now()""",
            request.execution_id,
            request.attempt_id,
        )
        if row is None:
            return False
        expected = (row["task_id"], row["owner"], row["generation"], row["stage"], row["operation_id"], row["identity"])
        observed = (
            request.task_id,
            request.owner,
            request.generation,
            request.stage,
            request.operation_id,
            request.identity,
        )
        if expected != observed:
            logger.warning("storage envelope does not match persisted intent")
            return False
        return True

    async def _heartbeat(self, request: ActivityRequest, token: UUID, running: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(15)  # asyncio uses a monotonic clock
            try:
                owned = await self.pool.fetchval("SELECT heartbeat_activity($1,$2)", request.attempt_id, token)
            except Exception:
                owned = False
            if not owned:
                running.cancel()
                return


def _json(payload: dict) -> str:
    return json.dumps(payload)


def _review_error_code(error: CanaryBlockedError | AccountSessionRequiredError) -> str:
    if isinstance(error, AccountSessionRequiredError):
        return "PHOTOS_ACCOUNT_SESSION_REQUIRED"
    return error.code if isinstance(error, UploadReviewRequiredError) else "USER_ACTION_REQUIRED"
