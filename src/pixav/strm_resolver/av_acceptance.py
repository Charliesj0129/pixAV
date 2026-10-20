"""Recording analysis for pixAV synthetic media, independent of DOM playback time.

FFmpeg provides decoding/capture. A visual binary time marker and changing
known tone make muted, frozen, wrong-tone, seek and resume failures measurable.
This module never reads the original media while checking a client recording.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from pixav.shared.media_file import open_media_file

WIDTH, HEIGHT, RATE = 640, 360, 48000
FRAME_BYTES = WIDTH * HEIGHT * 3
FFMPEG = "/usr/bin/ffmpeg"


class SyntheticAVSpec(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    method: Literal["pixav-synthetic-av-v1", "pixav-synthetic-av-v2"] = "pixav-synthetic-av-v2"
    identity: uuid.UUID
    duration_seconds: float = Field(gt=10, lt=1600, allow_inf_nan=False)
    tone_base_hz: int = Field(default=400, ge=300, le=800)
    visual_variant: int = Field(default=0, ge=0, le=20)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()

    def tone(self, seconds: float) -> int:
        return self.tone_base_hz + 90 * (int(seconds // 5) % 8)

    @property
    def marker_row(self) -> int:
        return 30 if self.method == "pixav-synthetic-av-v1" else 80


def marker_filter(spec: SyntheticAVSpec) -> str:
    row = spec.marker_row
    filters = [
        f"hue=h={spec.visual_variant * 37}",
        f"drawbox=x=16:y={row - 22}:w=608:h=44:color=cyan:t=fill",
        f"drawbox=x=56:y={row - 18}:w=528:h=36:color=black:t=fill",
    ]
    for bit in range(14):
        filters.append(
            f"drawbox=x={64 + bit * 32}:y={row - 14}:w=24:h=28:color=white:t=fill:"
            f"enable='mod(floor(t*10/{2**bit}),2)'"
        )
    checksum = "floor(t*10)+floor(t*10/16)+floor(t*10/256)+floor(t*10/4096)"
    for bit in range(2):
        filters.append(
            f"drawbox=x={64 + (14 + bit) * 32}:y={row - 14}:w=24:h=28:color=white:t=fill:"
            f"enable='mod(floor(({checksum})/{2**bit}),2)'"
        )
    filters.append(f"drawtext=fontcolor=white:fontsize=24:x=24:y={row + 34}:text='pixAV %{{pts\\:hms}}'")
    return ",".join(filters)


def generate_media(spec: SyntheticAVSpec, output: Path, *, four_k: bool = True) -> None:
    """Generate into a seeder-only directory; never prepopulate the downloader."""
    if output.exists():
        raise ValueError("synthetic media output already exists")
    filters = marker_filter(spec) + (",scale=3840:2160" if four_k else "")
    tone = f"aevalsrc='0.25*sin(2*PI*({spec.tone_base_hz}+90*mod(floor(t/5),8))*t)':s={RATE}"
    command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=25",
        "-f",
        "lavfi",
        "-i",
        tone,
        "-t",
        str(spec.duration_seconds),
        "-vf",
        filters,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-threads",
        "4",
        "-b:v",
        "8M",
        "-minrate",
        "8M",
        "-maxrate",
        "8M",
        "-bufsize",
        "16M",
        "-x264-params",
        "nal-hrd=cbr:force-cfr=1",
        "-g",
        "25",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(output),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=1800)  # noqa: S603 -- argv, no shell


def _pixel(frame: bytes, x: int, y: int) -> tuple[int, int, int]:
    offset = (y * WIDTH + x) * 3
    return tuple(frame[offset : offset + 3])  # type: ignore[return-value]


def decode_marker(frame: bytes, *, marker_row: int = 80) -> float:
    if len(frame) != FRAME_BYTES:
        raise ValueError("recording frame has wrong dimensions")
    r, g, b = _pixel(frame, 20, marker_row + 18)
    if r > 70 or g < 170 or b < 170:
        raise ValueError("synthetic marker anchor missing")
    bits = []
    for bit in range(16):
        rgb = _pixel(frame, 76 + bit * 32, marker_row)
        if max(rgb) < 70:
            bits.append(0)
        elif min(rgb) > 140:
            bits.append(1)
        else:
            raise ValueError("synthetic marker is unreadable")
    ticks = sum(value << bit for bit, value in enumerate(bits[:14]))
    checksum = (ticks + ticks // 16 + ticks // 256 + ticks // 4096) % 4
    if checksum != bits[14] + 2 * bits[15]:
        raise ValueError("synthetic marker checksum mismatch")
    return ticks / 10


def tone_measurement(pcm: bytes, expected_hz: int, candidates: tuple[int, ...]) -> dict:
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if len(samples) < RATE // 10:
        raise ValueError("recording audio missing or too short")
    values = [value / 32768 for value in samples]
    rms = math.sqrt(sum(value * value for value in values) / len(values))
    energies = {}
    for frequency in candidates:
        coefficient = 2 * math.cos(2 * math.pi * frequency / RATE)
        previous, earlier = 0.0, 0.0
        for value in values:
            current = value + coefficient * previous - earlier
            earlier, previous = previous, current
        energies[frequency] = max(0.0, previous**2 + earlier**2 - coefficient * previous * earlier)
    dominant = max(energies, key=lambda frequency: energies[frequency])
    other = max((energy for frequency, energy in energies.items() if frequency != expected_hz), default=0)
    expected = energies.get(expected_hz, 0)
    amplitude = 2 * math.sqrt(expected) / len(values)
    purity = amplitude**2 / max(2 * rms**2, 1e-10)
    if rms < 0.02 or amplitude < 0.03 or purity < 0.8 or dominant != expected_hz or expected < 4 * max(other, 1e-10):
        raise ValueError("recording is muted or contains the wrong tone")
    return {
        "audio_rms": rms,
        "tone_amplitude": amplitude,
        "dominant_hz": dominant,
        "tone_margin": expected / max(other, 1e-10),
        "tone_purity": purity,
    }


def picture_motion(first: bytes, last: bytes) -> float:
    """The moving picture must change outside the timecode and player controls."""
    differences: list[int] = []
    # Exclude both binary and human-readable timecodes in either recipe.
    for y in range(160, 330, 15):
        for x in range(40, 620, 20):
            left, right = _pixel(first, x, y), _pixel(last, x, y)
            differences.extend(abs(a - b) for a, b in zip(left, right, strict=True))
    motion = sum(differences) / len(differences)
    if motion < 1:
        raise ValueError("picture outside the time marker is frozen")
    return motion


def validate_progress(times: list[float], expected_seconds: float, window_seconds: float) -> dict:
    if len(times) < 8:
        raise ValueError("too few readable client frames")
    if abs(times[0] - expected_seconds) > 2:
        raise ValueError("seek or resume exceeds the two-second tolerance")
    deltas = [right - left for left, right in zip(times, times[1:], strict=False)]
    if any(delta < -0.11 or delta > 0.4 for delta in deltas):
        raise ValueError("client picture time jumps or goes backwards")
    progress = times[-1] - times[0]
    if progress < window_seconds * 0.65 or progress > window_seconds * 1.3:
        raise ValueError("client picture is frozen or advances at the wrong speed")
    return {"first_seconds": times[0], "last_seconds": times[-1], "frame_count": len(times)}


def analyze_window(recording: Path, spec: SyntheticAVSpec, window: dict) -> dict:
    """Analyze only the actual captured picture/audio; DOM clocks cannot pass it."""
    start, duration = float(window["recording_seconds"]), 4.0 if window["kind"] == "junction_crossing" else 1.2
    crop = window["crop"]
    if len(crop) != 4 or any(type(value) is not int or value < 0 for value in crop) or min(crop[:2]) < 1:
        raise ValueError("invalid recorded video geometry")
    width, height, x, y = crop
    raw = subprocess.run(  # noqa: S603 -- validated geometry; no shell
        [
            FFMPEG,
            "-v",
            "error",
            "-nostdin",
            "-ss",
            str(start),
            "-i",
            str(recording),
            "-t",
            str(duration),
            "-vf",
            f"crop={width}:{height}:{x}:{y},scale=640:360,fps=10",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    times = [
        decode_marker(raw[index : index + FRAME_BYTES], marker_row=spec.marker_row)
        for index in range(0, len(raw), FRAME_BYTES)
    ]
    result = validate_progress(times, float(window["expected_seconds"]), duration - 0.1)
    if (
        window["kind"] == "junction_crossing"
        and not result["first_seconds"] < window["boundary_seconds"] < result["last_seconds"]
    ):
        raise ValueError("recording does not continuously cross the segment boundary")
    result["picture_motion"] = picture_motion(raw[:FRAME_BYTES], raw[-FRAME_BYTES:])
    tones = tuple(spec.tone_base_hz + 90 * index for index in range(8))
    audio_checks = []
    # Three separate intervals catch audio loss within a checkpoint window.
    for offset in ((0.1, 0.5, 0.9, 1.5, 2.5, 3.5) if duration == 4.0 else (0.1, 0.5, 0.9)):
        index = round(offset * 10)
        source_time = times[index] + 0.1
        # Avoid a tone-change edge; analyze a shorter interval on its earlier side.
        if int(times[index] // 5) != int((times[index] + 0.2) // 5):
            continue
        pcm = subprocess.run(  # noqa: S603 -- recording path and numeric seek; no shell
            [
                FFMPEG,
                "-v",
                "error",
                "-nostdin",
                "-ss",
                str(start + offset),
                "-i",
                str(recording),
                "-t",
                "0.2",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(RATE),
                "-f",
                "s16le",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
        audio_checks.append(tone_measurement(pcm, spec.tone(source_time), tones))
    if len(audio_checks) < 2:
        raise ValueError("insufficient audio intervals away from tone changes")
    return {**window, **result, "audio_checks": audio_checks, "status": "PASS"}


def _artifact_sha256(path: Path) -> str:
    with open_media_file(path) as source:
        return source.facts()[0]


def verify_capture(
    directory: Path, spec: SyntheticAVSpec, expected_binding: dict, *, expected_junction_seconds: tuple[float, ...] = ()
) -> dict:
    """Recompute one capture; a receipt's PASS alone is never evidence.

    The caller must supply the current asset/release binding independently.
    This validates captured media only; it neither writes domain state nor
    authorizes cleanup.
    """
    paths = {name: directory / name for name in ("result.json", "trace.json", "analysis.json", "client.mkv")}
    if directory.is_symlink() or any(path.is_symlink() or not path.is_file() for path in paths.values()):
        raise ValueError("capture artifacts must be regular files")
    receipt = json.loads(paths["result.json"].read_text())
    trace = json.loads(paths["trace.json"].read_text())
    analysis = json.loads(paths["analysis.json"].read_text())
    if (
        receipt.get("method") != "automated-jellyfin-av-v1"
        or receipt.get("status") != "PASS"
        or receipt.get("binding") != expected_binding
        or not expected_binding
        or receipt.get("synthetic_spec_sha256") != spec.sha256
        or analysis.get("spec_sha256") != spec.sha256
        or trace.get("fault_injection") is not None
    ):
        raise ValueError("capture identity or recipe does not match")
    for filename, key in (
        ("client.mkv", "recording_sha256"),
        ("trace.json", "trace_sha256"),
        ("analysis.json", "analysis_sha256"),
    ):
        if _artifact_sha256(paths[filename]) != receipt.get(key):
            raise ValueError("capture artifact digest mismatch")
    for key in ("item_id", "session_id", "device_id", "client_version", "server_version", "application_version"):
        if not trace.get(key) or trace[key] != receipt.get(key):
            raise ValueError("capture client identity mismatch")
    windows = _validate_capture_windows(trace, spec)
    crossings = [window for window in windows if window["kind"] == "junction_crossing"]
    if tuple(window.get("boundary_seconds") for window in crossings) != expected_junction_seconds:
        raise ValueError("required manifest segment crossings missing or mismatched")
    if any(
        window["expected_seconds"] != boundary - 3
        for window, boundary in zip(crossings, expected_junction_seconds, strict=True)
    ):
        raise ValueError("segment crossing checkpoint starts at the wrong position")
    recomputed = [analyze_window(paths["client.mkv"], spec, window) for window in windows]
    if recomputed != analysis.get("checkpoints") or recomputed != receipt.get("checkpoints"):
        raise ValueError("capture analysis does not reproduce")
    return {"method": receipt["method"], "status": "PASS", "binding": expected_binding, "checkpoints": recomputed}


def _validate_capture_windows(trace: dict, spec: SyntheticAVSpec) -> list[dict]:
    windows = trace["checkpoints"]
    if not {"beginning", "middle", "end", "seek", "resume"} <= {window["kind"] for window in windows}:
        raise ValueError("required playback checkpoints missing")
    expected_positions = {
        "middle": spec.duration_seconds / 2,
        "end": spec.duration_seconds - 4,
        "seek": spec.duration_seconds * 0.3,
    }
    for kind, position in expected_positions.items():
        matching = [window for window in windows if window["kind"] == kind]
        if len(matching) != 1 or matching[0]["expected_seconds"] != position:
            raise ValueError("capture checkpoint differs from the required media position")
    resume = [window for window in windows if window["kind"] == "resume"]
    first_resume = trace.get("resume_first_seconds")
    if (
        len(resume) != 1
        or type(first_resume) not in (int, float)
        or abs(first_resume - resume[0]["expected_seconds"]) > 2
    ):
        raise ValueError("initial resume picture exceeds the two-second tolerance")
    beginning = [window for window in windows if window["kind"] == "beginning"]
    if len(beginning) != 1 or not 0 <= beginning[0]["expected_seconds"] <= 5:
        raise ValueError("beginning checkpoint is outside the first five seconds")
    return windows
