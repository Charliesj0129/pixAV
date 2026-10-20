"""Private reference extraction and repeatable candidate matching for real films.

The analysis never attests human sight/hearing. Static or silent samples are
retained and require supplemental identifiable windows before acceptance.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import subprocess
from contextlib import ExitStack
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pixav.shared.backup_files import create_backup_file
from pixav.shared.media_file import MediaFile, file_version, open_media_file, open_media_output

FFMPEG = "/usr/bin/ffmpeg"
FRAME_BYTES = 64 * 36


class RealAVSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    method: str = "pixav-real-av-v2"
    duration_seconds: float = Field(ge=30, allow_inf_nan=False)
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    junction_seconds: tuple[float, ...] = ()
    supplemental_seconds: tuple[float, ...] = ()

    @model_validator(mode="after")
    def timeline(self) -> RealAVSpec:
        if self.method != "pixav-real-av-v2":
            raise ValueError("unsupported real capture method")
        if tuple(sorted(set(self.junction_seconds))) != self.junction_seconds or any(
            not 5 < x < self.duration_seconds - 5 for x in self.junction_seconds
        ):
            raise ValueError("invalid real capture junction set")
        if tuple(sorted(set(self.supplemental_seconds))) != self.supplemental_seconds or any(
            not math.isfinite(x) or not 0 <= x <= self.duration_seconds - 10 for x in self.supplemental_seconds
        ):
            raise ValueError("invalid real capture supplemental set")
        return self


def file_digest(path: Path) -> str:
    with open_media_file(path) as source:
        return source.facts()[0]


def validate_capture_windows(trace: dict) -> RealAVSpec:
    """Require a complete new recording, including its fixed supplements."""
    spec = RealAVSpec.model_validate(trace["real_spec"])
    windows = trace["checkpoints"]
    required = {"beginning": 0, "middle": spec.duration_seconds / 2 - 5, "end": spec.duration_seconds - 10}
    for kind in (*required, "seek", "resume"):
        matched = [w for w in windows if w["kind"] == kind]
        if len(matched) != 1 or (kind in required and abs(matched[0]["expected_seconds"] - required[kind]) > 1e-6):
            raise ValueError("capture required window set changed")
    if tuple(w["expected_seconds"] for w in windows if w["kind"] == "supplemental") != spec.supplemental_seconds:
        raise ValueError("capture supplemental window set changed")
    if tuple(w["boundary_seconds"] for w in windows if w["kind"] == "junction_crossing") != spec.junction_seconds:
        raise ValueError("capture junction window set changed")
    previous_end = -1.0
    for window in windows:
        if window["kind"] not in {*required, "seek", "resume", "supplemental", "junction_crossing"}:
            raise ValueError("unsupported capture window")
        start, length, recorded = window["expected_seconds"], window["duration_seconds"], window["recording_seconds"]
        if (
            not all(math.isfinite(x) for x in (start, length, recorded))
            or start < 0
            or length < 10
            or start + length > spec.duration_seconds + 1e-6
            or recorded < previous_end
        ):
            raise ValueError("invalid or overlapping capture window")
        previous_end = recorded + length
        if window["kind"] == "junction_crossing" and not (
            start <= window["boundary_seconds"] - 5 and start + length >= window["boundary_seconds"] + 5
        ):
            raise ValueError("capture must continuously cross the junction")
    return spec


def save_private(path: Path, value: dict) -> None:
    with create_backup_file(path) as handle:
        json.dump(value, handle, indent=2, allow_nan=False)


def _decode(
    path: Path | MediaFile, start: float, duration: float, *, audio: bool, crop: list[int] | None = None
) -> bytes:
    with ExitStack() as files:
        source = path if isinstance(path, MediaFile) else files.enter_context(open_media_file(path))
        return _decode_input(source, start, duration, audio=audio, crop=crop)


def _decode_input(source: MediaFile, start: float, duration: float, *, audio: bool, crop: list[int] | None) -> bytes:
    source.assert_current()
    command = [
        FFMPEG,
        "-v",
        "error",
        "-nostdin",
        "-threads",
        "2",
        "-ss",
        str(start),
        "-i",
        f"/proc/self/fd/{source.descriptor}",
        "-t",
        str(duration),
    ]
    if audio:
        command += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"]
    else:
        filters = (["crop=" + ":".join(map(str, crop))] if crop else []) + ["fps=2", "scale=64:36", "format=gray"]
        command += ["-map", "0:v:0", "-an", "-vf", ",".join(filters), "-f", "rawvideo", "pipe:1"]
    result = subprocess.run(  # noqa: S603 -- fixed tool; locked descriptor, no shell
        command,
        capture_output=True,
        timeout=120,
        pass_fds=(source.descriptor,),
    )
    source.assert_current()
    if result.returncode or result.stderr or not result.stdout:
        raise ValueError("reference or recording decode failed")
    return result.stdout


def _frames(raw: bytes) -> list[bytes]:
    if not raw or len(raw) % FRAME_BYTES:
        raise ValueError("incomplete frame sample")
    return [raw[i : i + FRAME_BYTES] for i in range(0, len(raw), FRAME_BYTES)]


def _energy(raw: bytes) -> list[float]:
    samples = array.array("h")
    samples.frombytes(raw)
    return [math.sqrt(sum(x * x for x in samples[i : i + 800]) / 800) for i in range(0, len(samples) - 799, 800)]


def candidate_match(reference: bytes, recording: bytes, reference_start: float) -> dict:
    """Find a candidate offset at 0.5s resolution, retaining ambiguity."""
    ref, got = _frames(reference), _frames(recording)
    if len(got) < 18 or len(ref) < len(got):
        raise ValueError("recording does not contain the required ten-second sample")
    scores = []
    for offset in range(len(ref) - len(got) + 1):
        error = sum(abs(a - b) for i in range(0, len(got), 2) for a, b in zip(ref[offset + i], got[i], strict=True))
        scores.append((error / (math.ceil(len(got) / 2) * FRAME_BYTES * 255), offset))
    scores.sort()
    best, offset = scores[0]
    separated = [score for score, i in scores if abs(i - offset) >= 4]
    alternative = min(separated, default=best)
    movement = sum(abs(a - b) for a, b in zip(got[0], got[-1], strict=True)) / FRAME_BYTES / 255
    return {
        "candidate_content_seconds": reference_start + offset / 2,
        "sampling_uncertainty_seconds": 0.5,
        "visual_difference": best,
        "alternative_difference": alternative,
        "identifiable_candidate": best < 0.12 and alternative - best > 0.01 and movement > 0.005,
        "status": "REVIEW_REQUIRED",
    }


def audio_candidate(reference: bytes, recording: bytes) -> dict:
    """Correlate PCM envelopes at 10Hz; diagnostic only, never an attestation."""
    ref, got = _energy(reference), _energy(recording)
    if min(len(ref), len(got)) < 80 or max(ref, default=0) < 5 or max(got, default=0) < 5:
        return {"audio_candidate_offset_seconds": None, "audio_envelope_correlation": None}
    candidates = []
    for shift in range(-20, 21):
        left, right = (ref[shift:], got) if shift >= 0 else (ref, got[-shift:])
        length = min(len(left), len(right))
        left, right = left[:length], right[:length]
        mean_left, mean_right = sum(left) / length, sum(right) / length
        variance_left = sum((x - mean_left) ** 2 for x in left)
        variance_right = sum((x - mean_right) ** 2 for x in right)
        denominator = math.sqrt(variance_left * variance_right)
        correlation = (
            sum((x - mean_left) * (y - mean_right) for x, y in zip(left, right, strict=True)) / denominator
            if denominator
            else 0
        )
        candidates.append((correlation, shift))
    correlation, shift = max(candidates)
    return {"audio_candidate_offset_seconds": shift / 10, "audio_envelope_correlation": correlation}


def create_reference(source: Path, source_sha256: str, trace: dict, binding: dict, output: Path) -> dict:
    """Extract hash-checked, bounded references; never mount them into capture."""
    with open_media_file(source) as locked:
        return _create_reference(locked, source_sha256, trace, binding, output)


def _create_reference(source: MediaFile, source_sha256: str, trace: dict, binding: dict, output: Path) -> dict:
    if source.facts()[0] != source_sha256:
        raise ValueError("reference source hash mismatch")
    spec = validate_capture_windows(trace)
    if trace.get("binding") != binding:
        raise ValueError("reference asset binding mismatch")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    windows = []
    for index, window in enumerate(trace["checkpoints"]):
        start = max(0, window["expected_seconds"] - 5)
        length = min(window["duration_seconds"] + 10, spec.duration_seconds - start)
        clip = output / f"window-{index:03d}.mkv"
        digest = _reference_clip(source, clip, start, length)
        windows.append({"checkpoint": window, "reference_start_seconds": start, "file": clip.name, "sha256": digest})
    if source.facts()[0] != source_sha256:
        raise ValueError("reference source changed during extraction")
    receipt = {
        "method": "real-av-reference-v2",
        "binding": binding,
        "real_spec": spec.model_dump(mode="json"),
        "source_sha256": source_sha256,
        "source_size": os.fstat(source.descriptor).st_size,
        "windows": windows,
    }
    save_private(output / "reference.json", receipt)
    return receipt


def _reference_clip(source: MediaFile, clip: Path, start: float, length: float) -> str:
    source.assert_current()
    with open_media_output(clip, must_create=True) as output:
        result = subprocess.run(  # noqa: S603 -- fixed tool; hash-checked private input
            [
                FFMPEG,
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-threads",
                "2",
                "-ss",
                str(start),
                "-i",
                f"/proc/self/fd/{source.descriptor}",
                "-t",
                str(length),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-vf",
                "scale=640:-2",
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-c:a",
                "pcm_s16le",
                "-f",
                "matroska",
                f"/proc/self/fd/{output.descriptor}",
            ],
            capture_output=True,
            timeout=180,
            pass_fds=(source.descriptor, output.descriptor),
        )  # noqa: S603 -- fixed tool; hash-checked private input
        source.assert_current()
        if result.returncode or result.stderr:
            raise ValueError("reference extraction failed")
        digest, size = output.facts()
        if size == 0:
            raise ValueError("reference extraction produced no media")
        return digest


def analyze_capture(recording: Path, trace_path: Path, reference_path: Path) -> dict:
    """Bind every sample to the same locked recording and evidence session."""
    with ExitStack() as files:
        locked = [files.enter_context(open_media_file(path)) for path in (recording, trace_path, reference_path)]
        video, trace_file, reference_file = locked
        versions = [file_version(os.fstat(item.descriptor)) for item in locked]
        with trace_file.reader() as handle:
            trace_raw = handle.read()
        with reference_file.reader() as handle:
            reference_raw = handle.read()
        recording_sha = video.facts()[0]
        trace, reference = json.loads(trace_raw), json.loads(reference_raw)
        results = _analyze_capture(video, trace, reference, reference_path.parent)
        digests = (recording_sha, hashlib.sha256(trace_raw).hexdigest(), hashlib.sha256(reference_raw).hexdigest())
        for item, version, digest in zip(locked, versions, digests, strict=True):
            item.assert_current()
            if file_version(os.fstat(item.descriptor)) != version or item.facts()[0] != digest:
                raise ValueError("analysis input changed during the session")
        return {
            "method": "real-av-analysis-v2",
            "status": "REVIEW_REQUIRED",
            "binding": trace["binding"],
            "recording_sha256": recording_sha,
            "trace_sha256": digests[1],
            "reference_sha256": digests[2],
            "checkpoints": results,
        }


def _analyze_capture(recording: MediaFile, trace: dict, reference: dict, root: Path) -> list[dict]:
    spec = validate_capture_windows(trace)
    if trace.get("binding") != reference.get("binding") or spec != RealAVSpec.model_validate(reference["real_spec"]):
        raise ValueError("recording/reference identity mismatch")
    if [w["checkpoint"] for w in reference["windows"]] != trace["checkpoints"]:
        raise ValueError("reference sampling set changed")
    results = []
    for window in reference["windows"]:
        clip = root / window["file"]
        if clip.parent.resolve() != root.resolve():
            raise ValueError("reference clip changed")
        with open_media_file(clip) as source:
            if source.facts()[0] != window["sha256"]:
                raise ValueError("reference clip changed")
            result = _analyze_window(recording, source, window)
            if source.facts()[0] != window["sha256"]:
                raise ValueError("reference clip changed during analysis")
            results.append(result)
    return results


def _analyze_window(recording: MediaFile, clip: MediaFile, window: dict) -> dict:
    checkpoint = window["checkpoint"]
    length = checkpoint["duration_seconds"]
    got = _decode(recording, checkpoint["recording_seconds"], length, audio=False, crop=checkpoint["crop"])
    ref = _decode(clip, 0, length + 10, audio=False)
    match = candidate_match(ref, got, window["reference_start_seconds"])
    content_offset = match["candidate_content_seconds"] - window["reference_start_seconds"]
    reference_audio = _decode(clip, content_offset, length, audio=True)
    recorded_audio = _decode(recording, checkpoint["recording_seconds"], length, audio=True)
    ref_energy, got_energy = _energy(reference_audio), _energy(recorded_audio)
    # Preserve diagnostics for naturally silent samples; human listening and
    # content matching remain mandatory even for an identifiable candidate.
    return {
        **checkpoint,
        **match,
        "reference_pcm_sha256": hashlib.sha256(reference_audio).hexdigest(),
        "recording_pcm_sha256": hashlib.sha256(recorded_audio).hexdigest(),
        "reference_rms_peak": max(ref_energy, default=0),
        "recording_rms_peak": max(got_energy, default=0),
        "source_silent_candidate": max(ref_energy, default=0) < 5,
        **audio_candidate(reference_audio, recorded_audio),
    }
