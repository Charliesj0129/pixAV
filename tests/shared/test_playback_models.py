"""Materialization acceptance rules for PlayableAsset (BDD-066, 082, 083, 084)."""

from __future__ import annotations

import uuid

import pytest

from pixav.shared.playback_models import (
    MATERIALIZATION_METHOD,
    PlayableAsset,
    cache_object_name,
    validate_materialization,
)
from pixav.shared.storage_models import IntegrityError, RemoteAssetSegment

ASSET = uuid.uuid4()
STREAMS = [
    {"kind": "video", "codec": "h264", "width": 1920, "height": 1080},
    {"kind": "audio", "codec": "aac", "width": 0, "height": 0},
]
WHOLE = {"container": "mov,mp4", "size_bytes": 4096, "duration_seconds": 20.0, "sha256": "a" * 64, "streams": STREAMS}


def segment(index: int, start: float, end: float, digest: str, size: int = 2048) -> RemoteAssetSegment:
    return RemoteAssetSegment(
        asset_id=ASSET,
        segment_index=index,
        start_seconds=start,
        end_seconds=end,
        size_bytes=size,
        sha256=digest,
        local_path="",
    )


def observed(size: int, digest: str, **changes) -> dict:
    return {
        "container": "mov,mp4",
        "size_bytes": size,
        "duration_seconds": 20.0,
        "sha256": digest,
        "streams": STREAMS,
        **changes,
    }


def receipt(parts: list[RemoteAssetSegment], size: int, digest: str, **changes) -> dict:
    return {
        "method": MATERIALIZATION_METHOD,
        "segments": [
            {"index": p.segment_index, "size": p.size_bytes, "sha256": p.sha256, "method": "photos-original-browser"}
            for p in parts
        ],
        "junctions": "single-segment" if len(parts) == 1 else "PASS",
        "observed": observed(size, digest),
        "size": size,
        "sha256": digest,
        **changes,
    }


def test_single_segment_object_is_the_provider_original_bdd_066():
    whole = segment(0, 0.0, 20.0, "a" * 64, size=4096)

    accepted = validate_materialization(WHOLE, [whole], receipt([whole], 4096, "a" * 64))

    assert accepted.sha256 == "a" * 64 and accepted.size == 4096


def test_single_segment_object_with_other_bytes_is_refused_bdd_069():
    whole = segment(0, 0.0, 20.0, "a" * 64, size=4096)

    with pytest.raises(IntegrityError, match="provider original"):
        validate_materialization(WHOLE, [whole], receipt([whole], 4096, "b" * 64))


def test_joined_segments_become_playable_when_junctions_hold_bdd_083():
    parts = [segment(0, 0.0, 10.0, "1" * 64), segment(1, 10.0, 20.0, "2" * 64)]

    accepted = validate_materialization(WHOLE, parts, receipt(parts, 4100, "c" * 64))

    assert accepted.junctions == "PASS"


def test_first_segment_alone_is_not_the_complete_movie_bdd_082():
    parts = [segment(0, 0.0, 10.0, "1" * 64), segment(1, 10.0, 20.0, "2" * 64)]
    only_first = receipt(parts[:1], 2048, "1" * 64, junctions="PASS")

    with pytest.raises(IntegrityError, match="every segment"):
        validate_materialization(WHOLE, parts, only_first)


def test_missing_segment_never_becomes_ready_bdd_084():
    parts = [segment(0, 0.0, 10.0, "1" * 64), segment(1, 10.0, 20.0, "2" * 64)]
    report = receipt(parts, 4100, "c" * 64)
    report["segments"][1]["sha256"] = "9" * 64

    with pytest.raises(IntegrityError, match="durable manifest"):
        validate_materialization(WHOLE, parts, report)


def test_segment_not_fetched_as_provider_original_is_refused():
    parts = [segment(0, 0.0, 10.0, "1" * 64), segment(1, 10.0, 20.0, "2" * 64)]
    report = receipt(parts, 4100, "c" * 64)
    report["segments"][0]["method"] = "local-copy"

    with pytest.raises(IntegrityError, match="provider original"):
        validate_materialization(WHOLE, parts, report)


def test_timeline_gap_fails_the_junction_check_bdd_083():
    parts = [segment(0, 0.0, 9.0, "1" * 64), segment(1, 10.0, 20.0, "2" * 64)]

    with pytest.raises(IntegrityError, match="gap or overlap"):
        validate_materialization(WHOLE, parts, receipt(parts, 4100, "c" * 64))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"duration_seconds": 18.0}, "duration"),
        ({"container": "matroska,webm"}, "not MP4"),
        ({"streams": [STREAMS[0], {"kind": "audio", "codec": "ac3", "width": 0, "height": 0}]}, "streams"),
    ],
)
def test_joined_object_must_be_the_same_media(change, message):
    parts = [segment(0, 0.0, 10.0, "1" * 64), segment(1, 10.0, 20.0, "2" * 64)]
    report = receipt(parts, 4100, "c" * 64)
    report["observed"] = observed(4100, "c" * 64, **change)

    with pytest.raises(IntegrityError, match=message):
        validate_materialization(WHOLE, parts, report)


def test_joined_object_without_media_expectation_is_refused():
    parts = [segment(0, 0.0, 10.0, "1" * 64), segment(1, 10.0, 20.0, "2" * 64)]

    with pytest.raises(IntegrityError, match="expectation"):
        validate_materialization({}, parts, receipt(parts, 4100, "c" * 64))


@pytest.mark.parametrize(
    "change",
    [
        {"method": "photos-original-browser"},
        {"observed": observed(1, "c" * 64)},
        {"junctions": "PASS"},
        {"sha256": "not-a-digest"},
    ],
)
def test_receipt_that_does_not_describe_its_object_is_refused(change):
    whole = segment(0, 0.0, 20.0, "a" * 64, size=4096)

    with pytest.raises(IntegrityError):
        validate_materialization(WHOLE, [whole], receipt([whole], 4096, "a" * 64, **change))


def test_cache_object_is_named_by_identity_only():
    asset = PlayableAsset(video_id=uuid.uuid4(), remote_asset_id=uuid.uuid4())

    assert asset.cache_name == cache_object_name(asset.id) == f"{asset.id}.mp4"
    assert asset.state == "PREPARING"


def test_parsed_receipt_cannot_drop_unknown_authentication_version():
    from pixav.shared.playback_models import parse_receipt

    whole = segment(0, 0.0, 20.0, "a" * 64, size=4096)
    report = receipt([whole], 4096, "a" * 64)
    report["segments"][0]["receipt_version"] = "unsupported-account-contract"
    parsed = parse_receipt(report)
    with pytest.raises(IntegrityError, match="account receipt"):
        validate_materialization(WHOLE, [whole], parsed)
