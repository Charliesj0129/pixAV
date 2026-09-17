"""Storage activity contracts: what the worker may and may not conclude.

These use fakes for the provider. They prove the worker's reporting rules, not
that Google Photos behaves a certain way — live evidence is collected separately
and is not implied by anything asserted here.
"""

from __future__ import annotations

import uuid

import pytest

from pixav.pixel_injector.maestro_parts import UserActionRequiredError
from pixav.pixel_injector.photos_storage import MaestroSegmentUploader
from pixav.pixel_injector.storage_activity import QuotaExhaustedError, StorageActivityWorker
from pixav.shared.exceptions import UploadError
from pixav.shared.storage_models import RemoteAssetSegment
from pixav.shared.workflow import ActivityRequest

ASSET = uuid.uuid4()
SHARE = "https://photos.app.goo.gl/synthetic"
BACKUP_EVIDENCE = {"backed_up": True, "original_quality": True}

# What the prepared artifact is, as the authority recorded it when the asset was
# created, and what a cold read-back of it must therefore report seeing.
EXPECTED_FACTS = {
    "container": "mov,mp4",
    "size_bytes": 2048,
    "duration_seconds": 10.0,
    "sha256": "c" * 64,
    "streams": [
        {"kind": "video", "codec": "h264", "width": 1920, "height": 1080},
        {"kind": "audio", "codec": "aac", "width": 0, "height": 0},
    ],
}


def readback_receipt(**overrides):
    receipt = {
        "method": "photos-original-browser",
        "size": 2048,
        "sha256": "c" * 64,
        "observed": dict(EXPECTED_FACTS),
    }
    receipt.update(overrides)
    return receipt


def segment_row(**overrides):
    row = {
        "asset_id": ASSET,
        "segment_index": 0,
        "start_seconds": 0.0,
        "end_seconds": 10.0,
        "size_bytes": 2048,
        "sha256": "c" * 64,
        "local_path": "/staging/prepared.mp4",
        "media_info": {},
        "share_url": None,
        "account_id": uuid.uuid4(),
        "state": "prepared",
        "recovery": {},
        "verification": {},
        "uploaded_at": None,
        "usage_counted_at": None,
        "retry_not_before": None,
        "updated_at": None,
    }
    row.update(overrides)
    return row


class FakePool:
    def __init__(self, row, asset=None):
        self._row = row
        self._asset = asset or {"policy_version": "photos-original-v2", "expected": dict(EXPECTED_FACTS)}
        self.journalled: list[str] = []

    async def fetchrow(self, sql, *args):
        if "remote_asset_segments" in sql:
            return self._row
        if "remote_assets" in sql:
            return self._asset
        return {"id": args[0], "email": "operator@example.invalid", "status": "active"}

    async def fetchval(self, sql, *args):
        if "journal_segment" in sql:
            self.journalled.append(args[0])
            return True
        return True


def request(stage="upload", **overrides):
    payload = {
        "task_id": uuid.uuid4(),
        "execution_id": uuid.uuid4(),
        "attempt_id": uuid.uuid4(),
        "operation_id": uuid.uuid4(),
        "owner": uuid.uuid4(),
        "generation": 1,
        "stage": stage,
        "identity": f"{stage}:{ASSET}:0",
        "asset_id": ASSET,
        "segment_index": 0,
        "account_id": uuid.uuid4(),
    }
    payload.update(overrides)
    return ActivityRequest(**payload)


class Uploader:
    def __init__(self, outcome=None, share=SHARE, evidence=None):
        self._outcome = outcome
        self._share = share
        self._evidence = BACKUP_EVIDENCE if evidence is None else evidence

    async def upload(self, segment, account, journal):
        if self._outcome is not None:
            raise self._outcome
        return self._share, self._evidence


class Readback:
    def __init__(self, evidence):
        self._evidence = evidence

    async def read_back(self, segment):
        return self._evidence


def worker(pool, uploader=None, readback=None):
    return StorageActivityWorker(pool, uploader or Uploader(), readback or Readback({}))


async def test_upload_journals_intent_before_the_effect_bdd_003():
    pool = FakePool(segment_row())
    result = await worker(pool).execute(request(), uuid.uuid4())
    assert result.outcome == "success"
    assert pool.journalled, "an external effect was attempted without a recorded intent"


async def test_upload_success_reports_the_share_location_bdd_046():
    pool = FakePool(segment_row())
    result = await worker(pool).execute(request(), uuid.uuid4())
    assert result.share_url == SHARE
    assert result.evidence == BACKUP_EVIDENCE


async def test_ui_success_without_backup_evidence_is_not_reported_as_success_bdd_047():
    pool = FakePool(segment_row())
    activity = worker(pool, uploader=Uploader(evidence={"backed_up": True}))
    result = await activity.execute(request(), uuid.uuid4())
    assert result.outcome != "success"


async def test_non_photos_location_is_not_reported_as_success_bdd_047():
    pool = FakePool(segment_row())
    activity = worker(pool, uploader=Uploader(share="https://example.invalid/share"))
    result = await activity.execute(request(), uuid.uuid4())
    assert result.outcome != "success"


async def test_login_challenge_stops_automatic_submission_bdd_045():
    pool = FakePool(segment_row())
    activity = worker(pool, uploader=Uploader(UserActionRequiredError("device confirmation")))
    result = await activity.execute(request(), uuid.uuid4())
    assert result.outcome == "user_action"
    assert result.error_code == "USER_ACTION_REQUIRED"


async def test_quota_exhaustion_is_distinguished_from_failure_bdd_039_124():
    pool = FakePool(segment_row())
    activity = worker(pool, uploader=Uploader(QuotaExhaustedError("daily quota reached")))
    result = await activity.execute(request(), uuid.uuid4())
    assert result.outcome == "quota_exhausted"


async def test_unknown_remote_effect_is_not_reported_as_plain_failure_bdd_044_113():
    pool = FakePool(segment_row())
    activity = worker(pool, uploader=Uploader(UploadError("adb connection lost mid-transfer")))
    result = await activity.execute(request(), uuid.uuid4())
    assert result.outcome == "unknown_effect"


async def test_readback_hash_mismatch_fails_verification_bdd_054_055():
    row = segment_row(share_url=SHARE, state="backed_up")
    activity = worker(
        FakePool(row),
        readback=Readback(readback_receipt(sha256="d" * 64)),
    )
    result = await activity.execute(request("verify"), uuid.uuid4())
    assert result.outcome == "verification_failed"
    assert result.error_code == "INTEGRITY_MISMATCH"


async def test_readback_byte_count_mismatch_fails_verification_bdd_054():
    row = segment_row(share_url=SHARE, state="backed_up")
    activity = worker(
        FakePool(row),
        readback=Readback(readback_receipt(size=1)),
    )
    result = await activity.execute(request("verify"), uuid.uuid4())
    assert result.outcome == "verification_failed"


async def test_readback_without_independent_provenance_fails_bdd_052():
    row = segment_row(share_url=SHARE, state="backed_up")
    activity = worker(
        FakePool(row),
        readback=Readback(readback_receipt(method="local-staging-copy")),
    )
    result = await activity.execute(request("verify"), uuid.uuid4())
    assert result.outcome == "verification_failed"


async def test_matching_cold_readback_succeeds_bdd_052_053_054():
    row = segment_row(share_url=SHARE, state="backed_up")
    receipt = readback_receipt()
    activity = worker(FakePool(row), readback=Readback(receipt))
    result = await activity.execute(request("verify"), uuid.uuid4())
    assert result.outcome == "success"
    assert result.evidence == receipt


async def test_a_receipt_without_observed_media_is_refused_bdd_053():
    """Matching bytes are not the same claim as "this is still the film"."""
    row = segment_row(share_url=SHARE, state="backed_up")
    receipt = readback_receipt()
    receipt.pop("observed")
    activity = worker(FakePool(row), readback=Readback(receipt))

    result = await activity.execute(request("verify"), uuid.uuid4())

    assert result.outcome == "verification_failed"
    assert result.error_code == "INTEGRITY_MISMATCH"


async def test_observed_codecs_must_match_the_prepared_artifact_bdd_053():
    row = segment_row(share_url=SHARE, state="backed_up")
    observed = {
        **EXPECTED_FACTS,
        "streams": [
            {"kind": "video", "codec": "vp9", "width": 1920, "height": 1080},
            {"kind": "audio", "codec": "aac", "width": 0, "height": 0},
        ],
    }
    activity = worker(FakePool(row), readback=Readback(readback_receipt(observed=observed)))

    result = await activity.execute(request("verify"), uuid.uuid4())

    assert result.outcome == "verification_failed"


async def test_an_asset_created_under_v1_is_still_judged_by_v1_bdd_056():
    """Evidence collected under the old rules keeps its original meaning.

    Tightening a policy in place would strand every asset already in flight,
    which is exactly what recording the version with the asset prevents.
    """
    row = segment_row(share_url=SHARE, state="backed_up")
    receipt = readback_receipt()
    receipt.pop("observed")
    pool = FakePool(row, asset={"policy_version": "photos-original-v1", "expected": dict(EXPECTED_FACTS)})
    activity = worker(pool, readback=Readback(receipt))

    result = await activity.execute(request("verify"), uuid.uuid4())

    assert result.outcome == "success"


async def test_substituted_upload_environment_is_refused_bdd_043():
    """A generic direct upload must never stand in for the configured guest."""
    uploader = MaestroSegmentUploader(
        lambda account_id: None,
        owner="test-owner",
        flows=None,
        mode="local",
        staging_root=None,
    )
    segment = RemoteAssetSegment.model_validate(segment_row())
    with pytest.raises(UploadError):
        await uploader.upload(segment, None, None)


class LoopPool(FakePool):
    """A pool that can answer the claim, intent and report queries in turn."""

    def __init__(self, row, *, intent=None, claimed=True):
        super().__init__(row)
        self._intent = intent
        self._claimed = claimed
        self.reported: list[str] = []

    async def fetchrow(self, sql, *args):
        if "operation_intents" in sql:
            return self._intent
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        if "claim_activity" in sql:
            return self._claimed
        if "report_activity" in sql:
            self.reported.append(args[0])
            return True
        return await super().fetchval(sql, *args)


class FakeQueue:
    def __init__(self, payload):
        self._payload = payload
        self.acked: list[str] = []

    async def pop_claim(self, timeout=1):
        payload, self._payload = self._payload, None
        return (payload, "receipt-1") if payload is not None else None

    async def ack(self, receipt):
        self.acked.append(receipt)


def intent_for(req, **overrides):
    row = {
        "task_id": req.task_id,
        "owner": req.owner,
        "generation": req.generation,
        "stage": req.stage,
        "operation_id": req.operation_id,
        "identity": req.identity,
    }
    row.update(overrides)
    return row


async def test_empty_transport_does_nothing():
    pool = LoopPool(segment_row())
    assert await worker(pool).run_one(FakeQueue(None)) is False


async def test_envelope_not_matching_persisted_intent_is_dropped_bdd_019_020():
    req = request()
    pool = LoopPool(segment_row(), intent=intent_for(req, identity="upload:other:0"))
    queue = FakeQueue(req.model_dump(mode="json"))

    assert await worker(pool).run_one(queue) is True

    assert queue.acked == ["receipt-1"]
    assert pool.reported == [], "a mismatched envelope must not produce a result"


async def test_missing_intent_is_dropped_bdd_020():
    req = request()
    pool = LoopPool(segment_row(), intent=None)
    queue = FakeQueue(req.model_dump(mode="json"))

    assert await worker(pool).run_one(queue) is True

    assert pool.reported == []


async def test_losing_the_claim_stops_the_activity_bdd_040():
    req = request()
    pool = LoopPool(segment_row(), intent=intent_for(req), claimed=False)
    queue = FakeQueue(req.model_dump(mode="json"))

    assert await worker(pool).run_one(queue) is True

    assert pool.reported == [], "an unclaimed attempt must create no external effect"


async def test_non_storage_stage_on_the_storage_transport_is_dropped():
    req = request().model_dump(mode="json")
    req["stage"] = "download"
    pool = LoopPool(segment_row())
    queue = FakeQueue(req)

    assert await worker(pool).run_one(queue) is True

    assert queue.acked == ["receipt-1"] and pool.reported == []


async def test_invalid_envelope_is_dropped():
    pool = LoopPool(segment_row())
    queue = FakeQueue({"not": "an envelope"})

    assert await worker(pool).run_one(queue) is True

    assert queue.acked == ["receipt-1"]


async def test_claimed_activity_reports_its_result_bdd_046():
    req = request()
    pool = LoopPool(segment_row(), intent=intent_for(req))
    queue = FakeQueue(req.model_dump(mode="json"))

    assert await worker(pool).run_one(queue) is True

    assert len(pool.reported) == 1
    assert SHARE in pool.reported[0]
    assert queue.acked == ["receipt-1"]
