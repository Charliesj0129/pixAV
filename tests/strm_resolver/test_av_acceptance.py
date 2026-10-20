"""Synthetic recording checks reject quiet, wrong-tone, frozen and wrong-time output."""

import array
import math
import os
import uuid
from types import SimpleNamespace

import pytest

from pixav.shared.media_file import MediaFile, open_media_file
from pixav.strm_resolver.av_acceptance import (
    FRAME_BYTES,
    RATE,
    WIDTH,
    SyntheticAVSpec,
    analyze_window,
    decode_marker,
    marker_filter,
    picture_motion,
    tone_measurement,
    validate_progress,
)


def frame_at(seconds, *, marker_row=80):
    frame = bytearray(FRAME_BYTES)
    anchor = ((marker_row + 18) * WIDTH + 20) * 3
    frame[anchor : anchor + 3] = bytes([0, 255, 255])
    ticks = int(seconds * 10)
    checksum = (ticks + ticks // 16 + ticks // 256 + ticks // 4096) % 4
    for bit in range(16):
        value = (ticks >> bit) & 1 if bit < 14 else (checksum >> (bit - 14)) & 1
        offset = (marker_row * WIDTH + 76 + bit * 32) * 3
        frame[offset : offset + 3] = bytes([255 * value] * 3)
    return bytes(frame)


@pytest.mark.parametrize("seconds", [0, 1.2, 5, 37.5, 511.9, 1599.8])
def test_decodes_picture_time_independently_of_dom(seconds):
    assert decode_marker(frame_at(seconds)) == seconds


def test_bad_anchor_checksum_and_dimensions_fail():
    with pytest.raises(ValueError, match="dimensions"):
        decode_marker(b"wrong")
    with pytest.raises(ValueError, match="anchor"):
        decode_marker(bytes(FRAME_BYTES))
    bad = bytearray(frame_at(12.3))
    offset = (80 * WIDTH + 76 + 14 * 32) * 3
    bad[offset : offset + 3] = bytes([255 - bad[offset]] * 3)
    with pytest.raises(ValueError, match="checksum"):
        decode_marker(bytes(bad))


def pcm_tone(frequency, amplitude=0.25):
    return array.array(
        "h", [round(amplitude * 32767 * math.sin(2 * math.pi * frequency * i / RATE)) for i in range(RATE // 5)]
    ).tobytes()


def test_real_samples_distinguish_known_tone_from_wrong_or_muted_audio():
    result = tone_measurement(pcm_tone(400), 400, (400, 490, 580))
    assert result["dominant_hz"] == 400 and result["audio_rms"] > 0.1
    for pcm in (pcm_tone(490), pcm_tone(1500), pcm_tone(400, 0), pcm_tone(400, 0.001)):
        with pytest.raises(ValueError, match="muted|wrong tone"):
            tone_measurement(pcm, 400, (400, 490, 580))
    with pytest.raises(ValueError, match="missing"):
        tone_measurement(b"", 400, (400, 490))


def test_progress_and_two_second_seek_resume_tolerance():
    times = [30 + i / 10 for i in range(12)]
    assert validate_progress(times, 30, 1.1)["first_seconds"] == 30
    validate_progress(times, 28, 1.1)
    with pytest.raises(ValueError, match="tolerance"):
        validate_progress(times, 27.9, 1.1)
    with pytest.raises(ValueError, match="frozen"):
        validate_progress([30] * 12, 30, 1.1)
    with pytest.raises(ValueError, match="too few"):
        validate_progress(times[:3], 30, 1.1)
    with pytest.raises(ValueError, match="backwards"):
        validate_progress([30, 30.1, 30.2, 28, *times[4:]], 30, 1.1)


def test_synthetic_spec_binds_distinct_recipe_and_known_tone_changes():
    spec = SyntheticAVSpec(identity=uuid.uuid4(), duration_seconds=90)
    assert spec.tone(4.9) == 400 and spec.tone(5) == 490
    assert len(spec.sha256) == 64
    assert spec.sha256 != spec.model_copy(update={"visual_variant": 1}).sha256
    assert "mod(floor(t*10/8192),2)" in marker_filter(spec)


def test_v1_recipe_hash_and_marker_remain_recomputable():
    spec = SyntheticAVSpec(method="pixav-synthetic-av-v1", identity=uuid.UUID(int=1), duration_seconds=60)
    assert spec.sha256 == "500bf1c693e4303abd40899fae8b9707576af93c7b03e4c5aa87d7120dae7776"
    assert spec.marker_row == 30 and "y=8:w=608" in marker_filter(spec)
    assert decode_marker(frame_at(2.3, marker_row=30), marker_row=spec.marker_row) == 2.3
    current = spec.model_copy(update={"method": "pixav-synthetic-av-v2"})
    assert current.marker_row == 80 and current.sha256 != spec.sha256


def test_timecode_alone_cannot_hide_a_frozen_picture():
    with pytest.raises(ValueError, match="outside the time marker"):
        picture_motion(frame_at(1), frame_at(2))
    readable_counter = bytearray(frame_at(2))
    readable_counter[114 * WIDTH * 3 : 145 * WIDTH * 3] = bytes([50]) * (31 * WIDTH * 3)
    with pytest.raises(ValueError, match="outside the time marker"):
        picture_motion(frame_at(1), bytes(readable_counter))
    assert picture_motion(bytes(FRAME_BYTES), bytes([50]) * FRAME_BYTES) == 50


def test_synthetic_generator_exclusively_creates_and_inherits_its_output(tmp_path, monkeypatch):
    from pixav.strm_resolver import av_acceptance as module

    destination = tmp_path / "synthetic.mp4"

    def encode(command, **kwargs):
        assert destination.is_file() and destination.stat().st_mode & 0o777 == 0o600
        with pytest.raises(BlockingIOError), open_media_file(destination):
            pytest.fail("generator output was available before encoding finished")
        descriptor = int(command[-1].removeprefix("/proc/self/fd/"))
        assert descriptor in kwargs["pass_fds"] and command[-3:-1] == ["-f", "mp4"]
        os.pwrite(descriptor, b"synthetic encoded bytes", 0)
        return SimpleNamespace(stdout=b"", stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", encode)
    module.generate_media(SyntheticAVSpec(identity=uuid.uuid4(), duration_seconds=12), destination, four_k=False)
    assert destination.read_bytes() == b"synthetic encoded bytes"


def test_synthetic_generator_refuses_a_dangling_symlink_without_starting_an_encoder(tmp_path, monkeypatch):
    from pixav.strm_resolver import av_acceptance as module

    destination = tmp_path / "synthetic.mp4"
    missing = tmp_path / "foreign.mp4"
    destination.symlink_to(missing)
    started = []
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: started.append(True))
    with pytest.raises((OSError, ValueError)):
        module.generate_media(SyntheticAVSpec(identity=uuid.uuid4(), duration_seconds=12), destination, four_k=False)
    assert destination.is_symlink() and not missing.exists() and not started


@pytest.mark.parametrize("fault", [None, "mute", "wrong-tone", "freeze", "freeze-picture", "seek", "resume"])
def test_recording_analysis_rejects_audiovisual_faults(tmp_path, monkeypatch, fault):
    spec = SyntheticAVSpec(identity=uuid.uuid4(), duration_seconds=60)
    frames = []
    for index in range(12):
        frame = bytearray(frame_at(2 if fault == "freeze" else 2 + index / 10))
        if fault not in {"freeze", "freeze-picture"}:
            frame[120 * WIDTH * 3 :] = bytes([index * 5]) * ((360 - 120) * WIDTH * 3)
        frames.append(bytes(frame))
    raw = b"".join(frames)
    pcm = pcm_tone(490 if fault == "wrong-tone" else 400, 0 if fault == "mute" else 0.25)
    recording = tmp_path / "client.mkv"
    recording.write_bytes(b"synthetic recording input")

    def decode(command, **kwargs):
        argument = command[command.index("-i") + 1]
        assert argument.startswith("/proc/self/fd/")
        descriptor = int(argument.rsplit("/", 1)[1])
        assert descriptor in kwargs["pass_fds"]
        assert os.fstat(descriptor).st_ino == recording.stat().st_ino
        assert kwargs["check"] is True
        return SimpleNamespace(stdout=raw if "rawvideo" in command else pcm, stderr=b"")

    monkeypatch.setattr("pixav.strm_resolver.av_acceptance.subprocess.run", decode)
    window = {
        "kind": "resume" if fault == "resume" else "seek",
        "recording_seconds": 10,
        "expected_seconds": 30 if fault in {"seek", "resume"} else 2,
        "crop": [1280, 720, 0, 0],
    }
    if fault:
        with pytest.raises(ValueError):
            analyze_window(recording, spec, window)
    else:
        result = analyze_window(recording, spec, window)
        assert result["status"] == "PASS" and len(result["audio_checks"]) == 3
        assert result["first_seconds"] == 2 and result["frame_count"] == 12


@pytest.fixture
def capture_bundle(tmp_path, monkeypatch):
    import hashlib
    import json

    from pixav.strm_resolver.av_acceptance import verify_capture

    spec = SyntheticAVSpec(identity=uuid.uuid4(), duration_seconds=60)
    binding = {"release": "isolated-unit-contract", "playable_asset_id": str(uuid.uuid4())}
    windows = [
        {"kind": kind, "expected_seconds": seconds, "recording_seconds": index, "crop": [1280, 720, 0, 0]}
        for index, (kind, seconds) in enumerate(
            [("beginning", 2), ("middle", 30), ("seek", 18), ("resume", 20), ("end", 56)]
        )
    ]
    trace = {
        "checkpoints": windows,
        "fault_injection": None,
        "resume_first_seconds": 20,
        **{
            key: "contract"
            for key in ("item_id", "session_id", "device_id", "client_version", "server_version", "application_version")
        },
    }
    results = [{**window, "status": "PASS"} for window in windows]
    analysis = {"spec_sha256": spec.sha256, "checkpoints": results}
    (tmp_path / "client.mkv").write_bytes(b"private recording contract")
    calls = []

    def recompute(recording, recipe, window):
        assert isinstance(recording, MediaFile) and recording.path == tmp_path / "client.mkv" and recipe == spec
        calls.append(window["kind"])
        return {**window, "status": "PASS"}

    monkeypatch.setattr("pixav.strm_resolver.av_acceptance.analyze_window", recompute)
    receipt = {
        "method": "automated-jellyfin-av-v1",
        "status": "PASS",
        "binding": binding,
        "synthetic_spec_sha256": spec.sha256,
        "checkpoints": results,
        **{
            key: trace[key]
            for key in ("item_id", "session_id", "device_id", "client_version", "server_version", "application_version")
        },
    }

    def save():
        (tmp_path / "trace.json").write_text(json.dumps(trace))
        (tmp_path / "analysis.json").write_text(json.dumps(analysis))
        for name, key in (
            ("client.mkv", "recording_sha256"),
            ("trace.json", "trace_sha256"),
            ("analysis.json", "analysis_sha256"),
        ):
            receipt[key] = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        (tmp_path / "result.json").write_text(json.dumps(receipt))

    save()
    return tmp_path, spec, binding, receipt, trace, analysis, save, calls, verify_capture


def test_capture_receipt_requires_recomputation_and_current_binding(capture_bundle):
    directory, spec, binding, _, _, _, _, calls, verify = capture_bundle
    assert verify(directory, spec, binding)["status"] == "PASS"
    assert calls == ["beginning", "middle", "seek", "resume", "end"]
    with pytest.raises(ValueError, match="identity"):
        verify(directory, spec, {"release": "different"})
    (directory / "client.mkv").write_bytes(b"replaced recording")
    with pytest.raises(ValueError, match="digest"):
        verify(directory, spec, binding)


@pytest.mark.parametrize("fault", ["resume", "identity", "checkpoint", "analysis", "injected"])
def test_capture_rejects_forged_or_inconsistent_receipts(capture_bundle, fault):
    directory, spec, binding, receipt, trace, analysis, save, _, verify = capture_bundle
    if fault == "resume":
        trace["resume_first_seconds"] = 0
    elif fault == "identity":
        receipt["session_id"] = "different"
    elif fault == "checkpoint":
        trace["checkpoints"][1]["expected_seconds"] = 2
    elif fault == "analysis":
        analysis["checkpoints"] = []
    else:
        trace["fault_injection"] = "mute"
    save()
    with pytest.raises(ValueError):
        verify(directory, spec, binding)


def test_capture_pass_receipt_cannot_override_failed_recording_analysis(capture_bundle, monkeypatch):
    directory, spec, binding, _, _, _, _, _, verify = capture_bundle

    def failed(*args):
        raise ValueError("actual captured audio is muted")

    monkeypatch.setattr("pixav.strm_resolver.av_acceptance.analyze_window", failed)
    with pytest.raises(ValueError, match="muted"):
        verify(directory, spec, binding)


@pytest.mark.parametrize("boundary", [4.0, 1.0, 8.0])
def test_crossing_requires_continuous_recorded_picture_and_audio(tmp_path, monkeypatch, boundary):
    spec = SyntheticAVSpec(identity=uuid.uuid4(), duration_seconds=60)
    recording = tmp_path / "client.mkv"
    recording.write_bytes(b"synthetic recording input")
    frames = []
    for index in range(40):
        frame = bytearray(frame_at(2 + index / 10))
        frame[160 * WIDTH * 3 :] = bytes([index * 5]) * ((360 - 160) * WIDTH * 3)
        frames.append(bytes(frame))
    raw = b"".join(frames)

    def decode(command, **kwargs):
        if "rawvideo" in command:
            return SimpleNamespace(stdout=raw, stderr=b"")
        offset = float(command[command.index("-ss") + 1])
        return SimpleNamespace(stdout=pcm_tone(spec.tone(2 + offset + 0.1)), stderr=b"")

    monkeypatch.setattr("pixav.strm_resolver.av_acceptance.subprocess.run", decode)
    window = {
        "kind": "junction_crossing",
        "boundary_seconds": boundary,
        "recording_seconds": 0,
        "expected_seconds": 2,
        "crop": [1280, 720, 0, 0],
    }
    if boundary == 4:
        result = analyze_window(tmp_path / "client.mkv", spec, window)
        assert result["first_seconds"] < boundary < result["last_seconds"]
        assert len(result["audio_checks"]) >= 4
    else:
        with pytest.raises(ValueError, match="continuously cross"):
            analyze_window(tmp_path / "client.mkv", spec, window)


def test_segment_manifest_requires_every_crossing(capture_bundle):
    directory, spec, binding, _, _, _, _, _, verify = capture_bundle
    with pytest.raises(ValueError, match="manifest segment crossings"):
        verify(directory, spec, binding, expected_junction_seconds=(30.0,))


@pytest.mark.parametrize("fault", [None, "video-diagnostic", "audio-diagnostic", "video-replaced", "audio-replaced"])
def test_synthetic_window_retains_locked_recording_descriptor_between_samples(tmp_path, monkeypatch, fault):
    recording = tmp_path / "client.mkv"
    recording.write_bytes(b"synthetic recording input")
    spec = SyntheticAVSpec(identity=uuid.uuid4(), duration_seconds=60)
    frames = []
    for index in range(12):
        frame = bytearray(frame_at(2 + index / 10))
        frame[120 * WIDTH * 3 :] = bytes([index * 5]) * ((360 - 120) * WIDTH * 3)
        frames.append(bytes(frame))
    calls = []

    def decode(command, **kwargs):
        with pytest.raises(BlockingIOError), open_media_file(recording, exclusive=True):
            pytest.fail("recording writer entered during a decoder sample")
        argument = command[command.index("-i") + 1]
        assert argument.startswith("/proc/self/fd/")
        descriptor = int(argument.rsplit("/", 1)[1])
        assert descriptor in kwargs["pass_fds"]
        assert os.fstat(descriptor).st_ino == recording.stat().st_ino
        kind = "video" if "rawvideo" in command else "audio"
        calls.append(kind)
        if fault == kind + "-replaced":
            replacement = tmp_path / "replacement.mkv"
            replacement.write_bytes(b"uncooperative replacement")
            os.replace(replacement, recording)
        return SimpleNamespace(
            stdout=b"".join(frames) if kind == "video" else pcm_tone(400),
            stderr=b"decode error" if fault == kind + "-diagnostic" else b"",
        )

    monkeypatch.setattr("pixav.strm_resolver.av_acceptance.subprocess.run", decode)
    window = {"kind": "beginning", "recording_seconds": 0, "expected_seconds": 2, "crop": [640, 360, 0, 0]}
    before = recording.stat()
    if fault:
        with pytest.raises(ValueError):
            analyze_window(recording, spec, window)
        assert calls == (["video"] if fault.startswith("video") else ["video", "audio"])
    else:
        assert analyze_window(recording, spec, window)["status"] == "PASS"
        assert calls == ["video", "audio", "audio", "audio"]
        assert recording.stat().st_atime_ns == before.st_atime_ns
    with open_media_file(recording, exclusive=True):
        pass


def test_synthetic_verification_locks_the_entire_capture_session(capture_bundle, monkeypatch):
    directory, spec, binding, _, _, _, _, _, verify = capture_bundle
    checked = []

    def analyze(recording, recipe, window):
        for name in ("client.mkv", "result.json", "trace.json", "analysis.json"):
            with pytest.raises(BlockingIOError), open_media_file(directory / name, exclusive=True):
                pytest.fail("capture input released between validation and sampling")
        assert isinstance(recording, MediaFile)
        assert recording.path == directory / "client.mkv" and recipe == spec
        checked.append(window["kind"])
        return {**window, "status": "PASS"}

    monkeypatch.setattr("pixav.strm_resolver.av_acceptance.analyze_window", analyze)
    assert verify(directory, spec, binding)["status"] == "PASS"
    assert checked == ["beginning", "middle", "seek", "resume", "end"]
    for name in ("client.mkv", "result.json", "trace.json", "analysis.json"):
        with open_media_file(directory / name, exclusive=True):
            pass


def test_synthetic_verification_refuses_inputs_changed_after_sampling(capture_bundle, monkeypatch):
    directory, spec, binding, _, _, _, _, _, verify = capture_bundle

    def changed(recording, recipe, window):
        if window["kind"] == "end":
            (directory / "trace.json").write_bytes(b"uncooperative writer ignored the lock")
        return {**window, "status": "PASS"}

    monkeypatch.setattr("pixav.strm_resolver.av_acceptance.analyze_window", changed)
    with pytest.raises(ValueError, match="capture input changed"):
        verify(directory, spec, binding)
