"""Private review derivatives preserve continuous windows and unfilled attestations."""

import json
from pathlib import Path

import pytest

import scripts.review_jellyfin_av as review
from pixav.shared.backup_files import create_backup_file
from pixav.strm_resolver.real_av_acceptance import file_digest


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    recording, trace, reference, analysis, ref_clip = [
        tmp_path / name for name in ("client.mkv", "trace.json", "reference.json", "analysis.json", "ref.mkv")
    ]
    window = {
        "kind": "junction_crossing",
        "expected_seconds": 23,
        "recording_seconds": 30,
        "duration_seconds": 14,
        "boundary_seconds": 30,
    }
    for path, value in ((recording, "synthetic recording"), (ref_clip, "synthetic reference")):
        with create_backup_file(path) as handle:
            handle.write(value)
    report = {"checkpoints": [window]}
    for path, value in (
        (
            trace,
            {
                "binding": {"synthetic": True},
                "controls": [
                    {"kind": "seek", "target_seconds": 23, "recording_start_seconds": 20, "recording_end_seconds": 25},
                    {
                        "kind": "resume",
                        "target_seconds": 40,
                        "recording_start_seconds": 50,
                        "recording_end_seconds": 55,
                    },
                ],
            },
        ),
        (
            reference,
            {"windows": [{"file": ref_clip.name, "sha256": file_digest(ref_clip), "reference_start_seconds": 18}]},
        ),
        (analysis, report),
    ):
        with create_backup_file(path) as handle:
            json.dump(value, handle)
    monkeypatch.setattr(review, "analyze_capture", lambda *args: report)
    calls = []

    def extract(source, start, length, destination):
        calls.append((start, length))
        with create_backup_file(destination) as handle:
            handle.write("synthetic derivative")

    monkeypatch.setattr(review, "_review_clip", extract)
    return (recording, trace, reference, analysis), calls


def test_bundle_preserves_continuous_junction_and_operation_time_mapping(inputs, tmp_path):
    paths, calls = inputs
    result = review.create_review_bundle(*paths, tmp_path / "bundle")
    assert calls == [(30, 14), (18, 17), (48, 17)]
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["windows"][0]["review"]["visual_confirmation"] is None
    assert result["windows"][0]["review"]["audio_confirmation"] is None
    assert result["windows"][0]["boundary_seconds"] == 30
    assert result["artifacts"]["recording"]["sha256"] == file_digest(paths[0])
    assert (tmp_path / "bundle").stat().st_mode & 0o077 == 0
    assert all(p.stat().st_mode & 0o077 == 0 for p in (tmp_path / "bundle").iterdir())
    with pytest.raises(FileExistsError):
        review.create_review_bundle(*paths, tmp_path / "bundle")


def test_bundle_rejects_changed_analysis_and_public_recording(inputs, tmp_path):
    paths, _ = inputs
    paths[-1].write_text("{}")
    with pytest.raises(ValueError, match="recomputation"):
        review.create_review_bundle(*paths, tmp_path / "bundle")
    paths[0].chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        review.create_review_bundle(*paths, tmp_path / "bundle")


def test_bundle_cannot_claim_operation_review_from_legacy_trace(inputs, tmp_path):
    paths, _ = inputs
    paths[1].write_text(json.dumps({"binding": {"synthetic": True}}))
    result = review.create_review_bundle(*paths, tmp_path / "bundle")
    assert result["operation_review"] == "OPEN_MISSING_OPERATION_TIME_MAPPING"


def test_bundle_refuses_recording_replacement_during_derivation(inputs, tmp_path, monkeypatch):
    paths, _ = inputs
    original = review._review_clip

    def replace(source: Path, start, length, destination):
        original(source, start, length, destination)
        source.write_text("changed recording")

    monkeypatch.setattr(review, "_review_clip", replace)
    with pytest.raises(ValueError, match="source changed"):
        review.create_review_bundle(*paths, tmp_path / "bundle")
    assert not (tmp_path / "bundle/review.json").exists()
