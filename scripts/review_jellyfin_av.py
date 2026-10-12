#!/usr/bin/env python3
"""Extract private references, recompute capture analysis, or validate human review.

No queue/execution/upload writes. Capture/reference directories belong to the
operator; reference media must never be mounted into the playback or client.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from pixav.shared.backup_files import create_backup_file
from pixav.shared.client_playback_evidence import ClientPlaybackEvidenceV2
from pixav.strm_resolver.real_av_acceptance import (
    FFMPEG,
    RealAVSpec,
    analyze_capture,
    create_reference,
    file_digest,
    save_private,
)


def _private_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("review artifacts must be private regular files")
    return file_digest(path)


def _review_clip(recording: Path, start: float, length: float, destination: Path) -> None:
    result = subprocess.run(  # noqa: S603 -- fixed OSS tool, no shell; private derivative
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
            str(length),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(destination),
        ],
        capture_output=True,
        timeout=180,
    )
    if result.returncode:
        raise ValueError("review derivative extraction failed")


def create_review_bundle(recording: Path, trace: Path, reference: Path, analysis: Path, output: Path) -> dict:
    """Convenience clips keep original digests/time mapping; no attestation."""
    os.umask(0o077)
    artifacts = {
        name: {"path": str(path.resolve()), "sha256": _private_digest(path)}
        for name, path in (("recording", recording), ("trace", trace), ("reference", reference), ("analysis", analysis))
    }
    recomputed = analyze_capture(recording, trace, reference)
    if recomputed != json.loads(analysis.read_text()):
        raise ValueError("bundle analysis differs from recomputation")
    capture, references = json.loads(trace.read_text()), json.loads(reference.read_text())
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    windows = []
    for index, (window, ref) in enumerate(zip(recomputed["checkpoints"], references["windows"], strict=True)):
        clip = output / f"window-{index:03d}.mp4"
        _review_clip(recording, window["recording_seconds"], window["duration_seconds"], clip)
        source = reference.parent / ref["file"]
        if source.parent.resolve() != reference.parent.resolve() or _private_digest(source) != ref["sha256"]:
            raise ValueError("bundle reference clip changed")
        copied = output / f"reference-{index:03d}.mkv"
        with source.open("rb") as reader, create_backup_file(copied, binary=True) as writer:
            shutil.copyfileobj(reader, writer)
        windows.append(
            {
                "kind": window["kind"],
                "recording_seconds": window["recording_seconds"],
                "expected_seconds": window["expected_seconds"],
                "duration_seconds": window["duration_seconds"],
                "boundary_seconds": window.get("boundary_seconds"),
                "clip": clip.name,
                "clip_sha256": file_digest(clip),
                "reference": copied.name,
                "reference_sha256": ref["sha256"],
                "reference_start_seconds": ref["reference_start_seconds"],
                "analysis_candidate": window,
                "review": {
                    key: None
                    for key in (
                        "visual_confirmation",
                        "audio_confirmation",
                        "continuous",
                        "content_seconds",
                        "uncertainty_seconds",
                        "identifiable",
                        "source_static",
                        "operator",
                        "confirmed_at",
                    )
                },
                "status": "REVIEW_REQUIRED",
            }
        )
    controls = _bundle_controls(recording, capture, output)
    if any(_private_digest(Path(value["path"])) != value["sha256"] for value in artifacts.values()):
        raise ValueError("bundle source changed during extraction")
    manifest = {
        "version": "real-av-review-bundle-v1",
        "status": "REVIEW_REQUIRED",
        "binding": capture["binding"],
        "artifacts": artifacts,
        "windows": windows,
        "controls": controls,
        "operation_review": "REVIEW_REQUIRED" if controls else "OPEN_MISSING_OPERATION_TIME_MAPPING",
        "formal_evidence": "complete original recording, trace, reference and independent analysis",
    }
    save_private(output / "review.json", manifest)
    return manifest


def _bundle_controls(recording: Path, capture: dict, output: Path) -> list[dict]:
    controls = []
    for index, action in enumerate(capture.get("controls", [])):
        if not 0 <= action["recording_start_seconds"] <= action["recording_end_seconds"]:
            raise ValueError("invalid capture operation time mapping")
        start = max(0, action["recording_start_seconds"] - 2)
        length = action["recording_end_seconds"] + 10 - start
        if action["kind"] not in {"seek", "resume"} or not 10 <= length <= 180:
            raise ValueError("invalid capture operation time mapping")
        clip = output / f"control-{index:03d}.mp4"
        _review_clip(recording, start, length, clip)
        controls.append(
            {
                **action,
                "clip": clip.name,
                "clip_sha256": file_digest(clip),
                "recording_seconds": start,
                "duration_seconds": length,
                "status": "REVIEW_REQUIRED",
            }
        )
    return controls


def validate_review(evidence: dict, recording: Path, trace: Path, reference: Path, analysis: Path) -> dict:
    report = ClientPlaybackEvidenceV2.model_validate(evidence)
    for path, digest in (
        (recording, report.recording_sha256),
        (trace, report.trace_sha256),
        (reference, report.reference_sha256),
        (analysis, report.analysis_sha256),
    ):
        if path.is_symlink() or path.stat().st_mode & 0o077 or file_digest(path) != digest:
            raise ValueError("review artifact is changed or not private")
    recomputed = analyze_capture(recording, trace, reference)
    if recomputed != json.loads(analysis.read_text()):
        raise ValueError("analysis differs from independent recomputation")
    _validate_candidates(report, recomputed)
    capture = json.loads(trace.read_text())
    binding = {
        k: str(getattr(report, k))
        for k in ("video_id", "playable_asset_id", "remote_asset_id", "preparation_version", "sha256")
    }
    binding["size"] = report.size
    if capture.get("binding") != binding or any(
        capture.get(k) != getattr(report, k)
        for k in ("item_id", "session_id", "device_id", "client_version", "server_version", "saved_resume_seconds")
    ):
        raise ValueError("human review is bound to another client/asset")
    spec = capture["real_spec"]
    if json.loads(reference.read_text())["source_sha256"] != report.reference_source_sha256:
        raise ValueError("reference belongs to another prepared source")
    if spec["manifest_sha256"] != report.manifest_sha256 or spec["duration_seconds"] != report.duration_seconds:
        raise ValueError("review manifest changed")
    _validate_windows(report, capture)
    return report.model_dump(mode="json")


def _validate_candidates(report: ClientPlaybackEvidenceV2, recomputed: dict) -> None:
    for window, reviewed in zip(recomputed["checkpoints"], report.windows, strict=True):
        if not window["source_silent_candidate"] and window["recording_rms_peak"] < 5:
            raise ValueError("recording lost audible source audio")
        if reviewed.audio_confirmation == "source-silent" and not window["source_silent_candidate"]:
            raise ValueError("silent-source claim differs from reference")
        if (
            window["identifiable_candidate"]
            and abs(reviewed.content_seconds - window["candidate_content_seconds"]) > reviewed.uncertainty_seconds
        ):
            raise ValueError("human position differs from identifiable recomputed candidate")


def _validate_windows(report: ClientPlaybackEvidenceV2, capture: dict) -> None:
    spec = RealAVSpec.model_validate(capture["real_spec"])
    if tuple(w.expected_seconds for w in report.windows if w.kind == "supplemental") != spec.supplemental_seconds:
        raise ValueError("review supplemental set differs from capture spec")
    if list(report.junction_seconds) != capture["real_spec"]["junction_seconds"] or len(report.windows) != len(
        capture["checkpoints"]
    ):
        raise ValueError("review sampling set differs from recording")
    for reviewed, recorded in zip(report.windows, capture["checkpoints"], strict=True):
        for key in ("kind", "expected_seconds", "recording_seconds", "duration_seconds"):
            if getattr(reviewed, key) != recorded[key]:
                raise ValueError("review window changed")
        if reviewed.boundary_seconds != recorded.get("boundary_seconds"):
            raise ValueError("review junction changed")


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    reference = commands.add_parser("reference")
    reference.add_argument("--source", type=Path, required=True)
    reference.add_argument("--source-sha256", required=True)
    reference.add_argument("--trace", type=Path, required=True)
    reference.add_argument("--output", type=Path, required=True)
    for name in ("analyze", "validate", "bundle"):
        command = commands.add_parser(name)
        for option in ("recording", "trace", "reference", "analysis"):
            command.add_argument("--" + option, type=Path, required=True)
        if name == "validate":
            command.add_argument("--client-evidence", type=Path, required=True)
        if name == "bundle":
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "reference":
        trace = json.loads(args.trace.read_text())
        create_reference(args.source, args.source_sha256, trace, trace["binding"], args.output)
    elif args.command == "analyze":
        save_private(args.analysis, analyze_capture(args.recording, args.trace, args.reference))
    elif args.command == "bundle":
        create_review_bundle(args.recording, args.trace, args.reference, args.analysis, args.output)
    else:
        validate_review(
            json.loads(args.client_evidence.read_text()), args.recording, args.trace, args.reference, args.analysis
        )
    print("Private artifact validation complete; human conclusions are operator attestations.")


if __name__ == "__main__":
    main()
