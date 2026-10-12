"""BDD-135–143: a clock/old attestation cannot stand in for complete v2 review."""

import copy
import uuid
from datetime import datetime, timezone

import pytest

from pixav.shared.client_playback_evidence import (
    ClientPlaybackEvidence,
    ClientPlaybackEvidenceV2,
    client_matches_current_manifest,
    parse_client_playback_evidence,
    playback_manifest_digest,
)


def reviewed_v2():
    report = {
        "method": "jellyfin-client-v2",
        "video_id": str(uuid.uuid4()),
        "playable_asset_id": str(uuid.uuid4()),
        "remote_asset_id": str(uuid.uuid4()),
        "preparation_version": "photos-playback-v1",
        "sha256": "a" * 64,
        "size": 1000,
        "observed_at": "2026-01-01T00:00:00Z",
        "operator_confirmed_at": "2026-01-01T01:00:00Z",
        "client_version": "Chromium fixture",
        "server_version": "Jellyfin fixture",
        "operator": "human fixture",
        "recording_sha256": "b" * 64,
        "manifest_sha256": "c" * 64,
        "reference_sha256": "d" * 64,
        "reference_source_sha256": "a" * 64,
        "trace_sha256": "e" * 64,
        "analysis_sha256": "f" * 64,
        "duration_seconds": 8381.4615,
        "junction_seconds": [4190.5],
        "item_id": "fixture-item",
        "session_id": "fixture-session",
        "device_id": "fixture-device",
        "saved_resume_seconds": 2500,
        **{k: "PASS" for k in ("beginning_audio_video", "middle_audio_video", "end_audio_video", "seek", "resume")},
    }
    windows = [
        ("beginning", 0),
        ("middle", report["duration_seconds"] / 2 - 5),
        ("junction_crossing", 4183.5),
        ("seek", 2490),
        ("resume", 2500),
        ("end", report["duration_seconds"] - 10),
    ]
    report["windows"] = [
        {
            "kind": kind,
            "expected_seconds": pos,
            "content_seconds": pos,
            "recording_seconds": index * 20,
            "duration_seconds": 14 if kind == "junction_crossing" else 10,
            "uncertainty_seconds": 0.5,
            "visual_confirmation": "PASS",
            "audio_confirmation": "PASS",
            "identifiable": True,
            "continuous": True,
            **({"boundary_seconds": 4190.5} if kind == "junction_crossing" else {}),
        }
        for index, (kind, pos) in enumerate(windows)
    ]
    return report


def test_long_real_review_and_legacy_reading_remain_distinct():
    value = reviewed_v2()
    assert isinstance(parse_client_playback_evidence(value), ClientPlaybackEvidenceV2)
    legacy = {key: value[key] for key in ClientPlaybackEvidence.model_fields}
    legacy["method"] = "jellyfin-client-v1"
    assert type(parse_client_playback_evidence(legacy)) is ClientPlaybackEvidence
    legacy["method"] = "jellyfin-client-v2"
    with pytest.raises(ValueError):
        parse_client_playback_evidence(legacy)


@pytest.mark.parametrize(
    "defect",
    [
        "missing-junction",
        "duplicate-junction",
        "short-junction",
        "wrong-boundary",
        "wrong-middle",
        "missing-window",
        "seek-error",
        "resume-error",
        "uncertainty",
        "no-human",
        "no-audio",
        "frozen",
        "unidentifiable",
        "early-human",
        "nan",
    ],
)
def test_incomplete_or_false_review_rejected(defect):
    value = copy.deepcopy(reviewed_v2())
    windows = value["windows"]
    mutations = {
        "missing-junction": lambda: windows.pop(2),
        "duplicate-junction": lambda: windows.append(copy.deepcopy(windows[2])),
        "short-junction": lambda: windows[2].update(duration_seconds=10),
        "wrong-boundary": lambda: windows[2].update(boundary_seconds=4191.5),
        "wrong-middle": lambda: windows[1].update(expected_seconds=100),
        "missing-window": lambda: windows.pop(0),
        "seek-error": lambda: windows[3].update(content_seconds=2491.6),
        "resume-error": lambda: value.update(saved_resume_seconds=2504),
        "uncertainty": lambda: windows[3].update(uncertainty_seconds=2.1),
        "no-human": lambda: value.pop("operator_confirmed_at"),
        "no-audio": lambda: windows[0].update(audio_confirmation="REVIEW_REQUIRED"),
        "frozen": lambda: windows[0].update(continuous=False),
        "unidentifiable": lambda: windows[0].update(identifiable=False),
        "early-human": lambda: value.update(operator_confirmed_at="2025-01-01T00:00:00Z"),
        "nan": lambda: windows[0].update(content_seconds=float("nan")),
    }
    mutations[defect]()
    with pytest.raises(ValueError):
        ClientPlaybackEvidenceV2.model_validate(value)


@pytest.mark.asyncio
async def test_manifest_rechecked_for_repository_and_cleanup():
    value = reviewed_v2()
    segments = [
        {
            "asset_id": value["remote_asset_id"],
            "segment_index": i,
            "sha256": str(i) * 64,
            "size_bytes": 500,
            "start_seconds": start,
            "end_seconds": end,
            "media_info": {"codec": "fixture"},
        }
        for i, (start, end) in enumerate(((0.0, 4190.5), (4190.5, 8381.4615)))
    ]
    value["manifest_sha256"] = playback_manifest_digest(segments)
    client = ClientPlaybackEvidenceV2.model_validate(value)
    row = {
        "id": value["playable_asset_id"],
        "video_id": value["video_id"],
        "remote_asset_id": value["remote_asset_id"],
        "preparation_version": value["preparation_version"],
        "sha256": value["sha256"],
        "size_bytes": value["size"],
        "ready_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "checked_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }

    class Connection:
        async def fetchval(self, query, asset):
            return {"facts": {"sha256": "a" * 64}}

        async def fetch(self, query, asset):
            assert asset == row["remote_asset_id"]
            return segments

    assert await client_matches_current_manifest(client, row, Connection())
    segments[1]["sha256"] = "a" * 64
    assert not await client_matches_current_manifest(client, row, Connection())
    segments.pop()
    assert not await client_matches_current_manifest(client, row, Connection())
