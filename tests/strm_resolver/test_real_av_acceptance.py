"""Real footage matching produces review candidates, never human PASS."""

import pytest

from pixav.strm_resolver.real_av_acceptance import FRAME_BYTES, RealAVSpec, candidate_match, validate_capture_windows
from scripts.capture_jellyfin_av import _capture_spec


def test_real_capture_has_no_short_synthetic_duration_limit():
    spec = {"duration_seconds": 8381.4615, "manifest_sha256": "a" * 64, "junction_seconds": [4190.5]}
    assert isinstance(_capture_spec({"real_spec": spec}), RealAVSpec)
    with pytest.raises(ValueError, match="exactly one"):
        _capture_spec({"real_spec": spec, "synthetic_spec": {}})


def test_visual_offset_and_uncertainty_are_repeatable_without_claiming_pass():
    frames = [bytes([(i * 13) % 256]) * FRAME_BYTES for i in range(40)]
    result = candidate_match(b"".join(frames), b"".join(frames[10:30]), 100)
    assert result["candidate_content_seconds"] == 105
    assert result["sampling_uncertainty_seconds"] == 0.5
    assert result["status"] == "REVIEW_REQUIRED"


def test_static_or_frozen_content_cannot_prove_position():
    ref = bytes([50]) * FRAME_BYTES * 40
    result = candidate_match(ref, ref[: FRAME_BYTES * 20], 0)
    assert not result["identifiable_candidate"]
    assert result["status"] == "REVIEW_REQUIRED"
    with pytest.raises(ValueError, match="ten-second"):
        candidate_match(ref, ref[: FRAME_BYTES * 10], 0)


@pytest.mark.parametrize("positions", [(51,), (20, 10), (20, 20), (-1,), (float("nan"),), (float("inf"),)])
def test_supplements_are_fixed_ordered_finite_ten_second_windows(positions):
    with pytest.raises(ValueError, match="supplemental"):
        RealAVSpec(duration_seconds=60, manifest_sha256="a" * 64, supplemental_seconds=positions)


def complete_trace():
    spec = RealAVSpec(
        duration_seconds=60, manifest_sha256="a" * 64, junction_seconds=(30,), supplemental_seconds=(10, 40)
    )
    windows = [
        ("beginning", 0),
        ("middle", 25),
        ("junction_crossing", 23),
        ("seek", 18),
        ("supplemental", 10),
        ("supplemental", 40),
        ("resume", 28),
        ("end", 50),
    ]
    return {
        "real_spec": spec.model_dump(mode="json"),
        "checkpoints": [
            {
                "kind": kind,
                "expected_seconds": target,
                "duration_seconds": 14 if kind == "junction_crossing" else 10,
                "recording_seconds": index * 20,
                **({"boundary_seconds": 30} if kind == "junction_crossing" else {}),
            }
            for index, (kind, target) in enumerate(windows)
        ],
    }


def test_capture_validates_all_required_windows_and_preserves_old_empty_spec():
    trace = complete_trace()
    assert validate_capture_windows(trace).supplemental_seconds == (10, 40)
    trace["real_spec"].pop("supplemental_seconds")
    trace["checkpoints"] = [w for w in trace["checkpoints"] if w["kind"] != "supplemental"]
    assert validate_capture_windows(trace).supplemental_seconds == ()


@pytest.mark.parametrize("kind", ["beginning", "middle", "end", "seek", "resume", "junction_crossing", "supplemental"])
def test_missing_window_or_junction_is_refused(kind):
    trace = complete_trace()
    trace["checkpoints"] = [w for w in trace["checkpoints"] if w["kind"] != kind]
    with pytest.raises(ValueError, match="window set"):
        validate_capture_windows(trace)


def test_junction_must_remain_continuous_and_capture_may_not_overlap():
    trace = complete_trace()
    trace["checkpoints"][2]["expected_seconds"] = 26
    with pytest.raises(ValueError, match="continuously"):
        validate_capture_windows(trace)
    trace = complete_trace()
    trace["checkpoints"][1]["recording_seconds"] = 5
    with pytest.raises(ValueError, match="overlapping"):
        validate_capture_windows(trace)
