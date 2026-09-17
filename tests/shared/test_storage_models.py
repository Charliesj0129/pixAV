"""Verification policy rules: what may and may not become durable evidence."""

from __future__ import annotations

import json
import uuid

import pytest

from pixav.media_loader.preparation import MediaFacts, timeline_defect
from pixav.shared.storage_models import (
    CURRENT_POLICY_VERSION,
    IntegrityError,
    PhotosOriginalV2,
    RemoteAssetSegment,
    VerificationPolicy,
    current_policy,
    policy_for,
)

POLICY = VerificationPolicy()
DIGEST = "c" * 64


def segment(**changes):
    return RemoteAssetSegment(
        **{
            **dict(
                asset_id=uuid.UUID(int=1),
                segment_index=0,
                start_seconds=0.0,
                end_seconds=10.0,
                size_bytes=2048,
                sha256=DIGEST,
                local_path="/staging/prepared.mp4",
            ),
            **changes,
        }
    )


def facts(**changes):
    return MediaFacts(
        **{
            **dict(
                container="mov,mp4",
                size_bytes=2048,
                duration_seconds=10.0,
                sha256=DIGEST,
                streams=(
                    {"kind": "video", "codec": "h264", "width": 1920, "height": 1080},
                    {"kind": "audio", "codec": "aac"},
                ),
            ),
            **changes,
        }
    )


def receipt(**changes):
    return {**dict(method="photos-original-browser", size=2048, sha256=DIGEST), **changes}


def test_policy_version_is_recorded_so_evidence_cannot_be_reinterpreted():
    assert POLICY.version == "photos-original-v1"


@pytest.mark.parametrize(
    "url",
    ["https://photos.app.goo.gl/abc", "https://photos.google.com/share/abc"],
)
def test_photos_share_locations_are_accepted(url):
    POLICY.validate_share_location(url)


@pytest.mark.parametrize(
    "url",
    ["https://example.invalid/share/abc", "pixav-local://abc", "http://photos.app.goo.gl/abc"],
)
def test_other_hosts_are_not_photos_locations_bdd_047(url):
    with pytest.raises(IntegrityError):
        POLICY.validate_share_location(url)


@pytest.mark.parametrize(
    "evidence",
    [{}, {"backed_up": True}, {"original_quality": True}, {"backed_up": False, "original_quality": True}],
)
def test_incomplete_backup_evidence_is_rejected_bdd_047(evidence):
    with pytest.raises(IntegrityError):
        POLICY.validate_backup_evidence(evidence)


def test_full_original_readback_is_accepted_bdd_054():
    POLICY.validate_segment_readback(segment(), receipt())


def test_readback_from_a_local_copy_is_rejected_bdd_052():
    with pytest.raises(IntegrityError):
        POLICY.validate_segment_readback(segment(), receipt(method="local-staging-copy"))


def test_readback_byte_count_mismatch_is_rejected_bdd_054():
    with pytest.raises(IntegrityError):
        POLICY.validate_segment_readback(segment(), receipt(size=1))


def test_readback_hash_mismatch_is_never_relaxed_bdd_054():
    with pytest.raises(IntegrityError):
        POLICY.validate_segment_readback(segment(), receipt(sha256="d" * 64))


def test_asset_readback_consistency_bdd_053():
    POLICY.validate_asset_readback(facts(), facts())


def test_asset_codec_mismatch_is_rejected_bdd_053():
    observed = facts(
        streams=(
            {"kind": "video", "codec": "vp9", "width": 1920, "height": 1080},
            {"kind": "audio", "codec": "aac"},
        )
    )
    with pytest.raises(IntegrityError):
        POLICY.validate_asset_readback(facts(), observed)


def test_asset_duration_beyond_tolerance_is_rejected_bdd_053():
    with pytest.raises(IntegrityError):
        POLICY.validate_asset_readback(facts(), facts(duration_seconds=10.5))


def test_segment_filename_carries_its_own_identity():
    """A segment from another manifest cannot be mistaken for this one."""
    name = segment().filename
    assert name.startswith(f"pixav-{uuid.UUID(int=1)}-part-0-")
    assert name.endswith(".mp4")
    assert segment(sha256="d" * 64).filename != name


# ── photos-original-v2: the manifest as a whole, not only its bytes ──────────

V2 = PhotosOriginalV2()


def whole_receipt(**changes):
    """A read-back receipt for a segment that stands for the entire artifact."""
    return {**receipt(), "observed": json.loads(facts().model_dump_json()), **changes}


def part(index, start, end, **changes):
    return segment(segment_index=index, start_seconds=start, end_seconds=end, **changes)


def test_the_current_policy_is_v2_and_v1_stays_available_bdd_056():
    """A version is frozen when it ships; a stricter rule gets a new number."""
    assert current_policy().version == CURRENT_POLICY_VERSION == "photos-original-v2"
    assert policy_for("photos-original-v1").version == "photos-original-v1"
    assert policy_for("photos-original-v2") is current_policy()


def test_evidence_under_an_unknown_policy_is_never_interpreted_bdd_056():
    with pytest.raises(IntegrityError):
        policy_for("photos-original-v99")


def test_v2_checks_the_whole_artifact_when_one_segment_carries_it_bdd_053():
    V2.validate_segment_media(segment(), facts().model_dump(mode="json"), whole_receipt())


def test_v2_refuses_a_receipt_that_observed_nothing_bdd_053():
    """Byte identity cannot show that the bytes still decode to the film."""
    with pytest.raises(IntegrityError, match="observed media facts"):
        V2.validate_segment_media(segment(), facts().model_dump(mode="json"), receipt())


def test_v1_still_reads_its_own_receipts_bdd_056():
    """The old rules keep their meaning; they are not retroactively tightened."""
    POLICY.validate_segment_media(segment(), facts().model_dump(mode="json"), receipt())
    POLICY.validate_manifest_timeline(facts().model_dump(mode="json"), [segment()])


def test_v2_rejects_a_whole_artifact_whose_codecs_changed_bdd_053():
    observed = facts(
        streams=(
            {"kind": "video", "codec": "vp9", "width": 1920, "height": 1080},
            {"kind": "audio", "codec": "aac"},
        )
    )
    with pytest.raises(IntegrityError):
        V2.validate_segment_media(
            segment(), facts().model_dump(mode="json"), whole_receipt(observed=json.loads(observed.model_dump_json()))
        )


def test_v2_checks_a_split_segment_against_its_own_span_bdd_053():
    """A part is not the film, so it answers for its span, not the duration."""
    piece = part(1, 10.0, 25.0, sha256="d" * 64, size_bytes=1024)
    observed = facts(duration_seconds=15.0, sha256="d" * 64, size_bytes=1024)

    V2.validate_segment_media(
        piece,
        facts(duration_seconds=25.0).model_dump(mode="json"),
        whole_receipt(observed=json.loads(observed.model_dump_json())),
    )

    drifted = facts(duration_seconds=16.0, sha256="d" * 64, size_bytes=1024)
    with pytest.raises(IntegrityError, match="segment duration"):
        V2.validate_segment_media(
            piece,
            facts(duration_seconds=25.0).model_dump(mode="json"),
            whole_receipt(observed=json.loads(drifted.model_dump_json())),
        )


def test_v2_refuses_an_observation_of_different_bytes_bdd_053():
    """The probe has to describe the very bytes whose hash was checked."""
    piece = part(1, 10.0, 25.0, sha256="d" * 64, size_bytes=1024)
    elsewhere = facts(duration_seconds=15.0, sha256="e" * 64, size_bytes=1024)
    with pytest.raises(IntegrityError, match="different bytes"):
        V2.validate_segment_media(
            piece,
            facts(duration_seconds=25.0).model_dump(mode="json"),
            whole_receipt(observed=json.loads(elsewhere.model_dump_json())),
        )


def test_v2_accepts_a_manifest_that_covers_the_film_exactly_once_bdd_056():
    expected = facts(duration_seconds=30.0).model_dump(mode="json")
    V2.validate_manifest_timeline(expected, [part(0, 0.0, 10.0), part(1, 10.0, 30.0)])


@pytest.mark.parametrize(
    ("segments", "defect"),
    [
        ([part(0, 0.0, 10.0), part(1, 12.0, 30.0)], "gap or overlap"),
        ([part(0, 0.0, 15.0), part(1, 10.0, 30.0)], "gap or overlap"),
        ([part(0, 2.0, 10.0), part(1, 10.0, 30.0)], "start at the beginning"),
        ([part(0, 0.0, 10.0), part(1, 10.0, 25.0)], "full media duration"),
        ([part(0, 0.0, 10.0), part(2, 10.0, 30.0)], "contiguous zero-based"),
    ],
)
def test_v2_refuses_a_manifest_that_does_not_reconstruct_the_film_bdd_056(segments, defect):
    """The plan-time junction rule is re-applied to what was actually stored."""
    expected = facts(duration_seconds=30.0).model_dump(mode="json")
    with pytest.raises(IntegrityError, match=defect):
        V2.validate_manifest_timeline(expected, segments)


def test_v2_and_segment_plan_share_one_definition_of_covering_the_film_bdd_056():
    """Two rules that could drift apart would be two different guarantees."""
    assert timeline_defect([(0, 0.0, 10.0), (1, 10.0, 30.0)], 30.0, tolerance=0.05) is None
    assert "gap or overlap" in str(timeline_defect([(0, 0.0, 10.0), (1, 12.0, 30.0)], 30.0, tolerance=0.05))


def test_an_asset_with_no_recorded_expectation_can_never_be_verified_bdd_053():
    with pytest.raises(IntegrityError, match="media expectation"):
        V2.validate_manifest_timeline({}, [segment()])
    with pytest.raises(IntegrityError, match="media expectation"):
        V2.validate_segment_media(segment(), {}, whole_receipt())
