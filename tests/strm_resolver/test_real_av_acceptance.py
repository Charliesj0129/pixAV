"""Real footage matching produces review candidates, never human PASS."""

import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from pixav.shared.media_file import MediaFile, open_media_file
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


def test_sample_decoder_passes_its_locked_input_to_the_child(tmp_path, monkeypatch):
    from pixav.strm_resolver import real_av_acceptance as module

    source = tmp_path / "recording.mkv"
    source.write_bytes(b"synthetic recording")

    def decode(command, **kwargs):
        with pytest.raises(BlockingIOError), open_media_file(source, exclusive=True):
            pytest.fail("sample decoder did not retain its input")
        argument = command[command.index("-i") + 1]
        assert argument.startswith("/proc/self/fd/")
        assert int(argument.rsplit("/", 1)[1]) in kwargs["pass_fds"]
        return SimpleNamespace(returncode=0, stdout=b"decoded bytes", stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", decode)
    assert module._decode(source, 0, 10, audio=False) == b"decoded bytes"
    with open_media_file(source, exclusive=True):
        pass


@pytest.mark.parametrize(
    "returncode,stdout,stderr", [(1, b"", b""), (0, b"sample", b"error diagnostic"), (0, b"", b"")]
)
def test_sample_decoder_refuses_failed_empty_or_diagnostic_output(tmp_path, monkeypatch, returncode, stdout, stderr):
    from pixav.strm_resolver import real_av_acceptance as module

    source = tmp_path / "recording.mkv"
    source.write_bytes(b"synthetic recording")
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    )
    with pytest.raises(ValueError, match="decode failed"):
        module._decode(source, 0, 10, audio=True)
    with open_media_file(source, exclusive=True):
        pass


def reference_fixture(tmp_path, monkeypatch):
    from pixav.strm_resolver import real_av_acceptance as module

    source = tmp_path / "prepared.mp4"
    source.write_bytes(b"synthetic prepared source")
    trace = complete_trace()
    trace["binding"] = {"fixture": "synthetic"}
    for checkpoint in trace["checkpoints"]:
        checkpoint["crop"] = [640, 360, 0, 0]
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    calls = []

    def extract(command, **kwargs):
        with pytest.raises(BlockingIOError), open_media_file(source, exclusive=True):
            pytest.fail("reference extraction released the prepared source between samples")
        input_path, output_path = command[command.index("-i") + 1], command[-1]
        assert input_path.startswith("/proc/self/fd/") and output_path.startswith("/proc/self/fd/")
        input_fd, output_fd = (int(path.rsplit("/", 1)[1]) for path in (input_path, output_path))
        assert input_fd in kwargs["pass_fds"] and output_fd in kwargs["pass_fds"]
        assert os.fstat(output_fd).st_mode & 0o777 == 0o600
        os.write(output_fd, b"synthetic reference clip")
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", extract)
    output = tmp_path / "reference"
    previous_umask = os.umask(0o022)
    try:
        report = module.create_reference(source, expected, trace, trace["binding"], output)
        assert os.umask(0o022) == 0o022, "reference extraction changed the process umask"
    finally:
        os.umask(previous_umask)
    return source, trace, output, report, calls


def test_reference_source_and_each_output_remain_locked_and_private(tmp_path, monkeypatch):
    source, trace, output, report, calls = reference_fixture(tmp_path, monkeypatch)
    assert len(calls) == len(report["windows"]) == len(trace["checkpoints"])
    assert report["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert (output / "reference.json").stat().st_mode & 0o777 == 0o600
    for window in report["windows"]:
        assert (output / window["file"]).stat().st_mode & 0o777 == 0o600
    with open_media_file(source, exclusive=True):
        pass


def test_analysis_holds_recording_and_evidence_for_the_entire_multi_sample_session(tmp_path, monkeypatch):
    from pixav.strm_resolver import real_av_acceptance as module

    _, trace, output, report, _ = reference_fixture(tmp_path, monkeypatch)
    recording = tmp_path / "recording.mkv"
    recording.write_bytes(b"synthetic captured recording")
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(json.dumps(trace))
    reference_path = output / "reference.json"
    inspected = []

    def decode(source, start, duration, *, audio, crop=None):
        for path in (recording, trace_path, reference_path):
            with pytest.raises(BlockingIOError), open_media_file(path, exclusive=True):
                pytest.fail("an offline session input was released before analysis completed")
        assert isinstance(source, MediaFile)
        with pytest.raises(BlockingIOError), open_media_file(source.path, exclusive=True):
            pytest.fail("reference clip was unlocked between hash and decode")
        inspected.append(source.path)
        if audio:
            return b"\0\0" * 80000
        return bytes([50]) * FRAME_BYTES * (20 if source.path == recording else 40)

    monkeypatch.setattr(module, "_decode", decode)
    result = module.analyze_capture(recording, trace_path, reference_path)
    assert result["status"] == "REVIEW_REQUIRED" and len(result["checkpoints"]) == len(report["windows"])
    assert len(inspected) == 4 * len(report["windows"])
    for path in (recording, trace_path, reference_path):
        with open_media_file(path, exclusive=True):
            pass


def test_failed_reference_retains_private_work_and_never_attests_partial_results(tmp_path, monkeypatch):
    from pixav.strm_resolver import real_av_acceptance as module

    source = tmp_path / "prepared.mp4"
    source.write_bytes(b"synthetic prepared source")
    trace = complete_trace()
    trace["binding"] = {"fixture": "synthetic"}

    def failure(command, **kwargs):
        output_fd = int(command[-1].rsplit("/", 1)[1])
        os.write(output_fd, b"unfinished evidence")
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"synthetic failure")

    monkeypatch.setattr(module.subprocess, "run", failure)
    output = tmp_path / "reference"
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="extraction failed"):
        module.create_reference(source, expected, trace, trace["binding"], output)
    partial = output / "window-000.mkv"
    assert partial.read_bytes() == b"unfinished evidence" and partial.stat().st_mode & 0o777 == 0o600
    assert not (output / "reference.json").exists()
    with pytest.raises(FileExistsError):
        module.create_reference(source, expected, trace, trace["binding"], output)
    assert partial.read_bytes() == b"unfinished evidence"
