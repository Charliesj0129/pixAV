"""Build one playable object from a durable remote manifest, in isolation.

This is the entry point of the playback materialization container. Like the
storage read-back, it runs image code and mounts only ``/work``: no
database credential, no queue, no staging root, no playback cache. Segment
bytes reach it only as Photos originals, fetched here or placed into
``/work/cloud`` by the worker as hard links to files the storage read-back
already fetched cold and hash-checked, each beside its original receipt.

A single segment is the provider original, so the object is that file. Several
segments are joined by a stream copy with explicit spans, never re-encoded.
The manifest arrives on stdin and the receipt leaves on stdout; a failure
prints only the exception type, because a message could carry a location.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from pixav.media_loader.preparation import MediaFacts, PreparationPolicy, timeline_defect
from pixav.media_loader.video_parts import PartMedia, contained_file, require_space, retained_temporary
from pixav.pixel_injector.parts_download import download_original, observed_media
from pixav.shared.playback_models import MATERIALIZATION_METHOD, validate_materialization
from pixav.shared.storage_models import IntegrityError, RemoteAssetSegment

WORK_ROOT = Path("/work")
_OUTPUT_NAME = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.mp4$")


def _segments(manifest: dict) -> list[RemoteAssetSegment]:
    segments = sorted(
        (RemoteAssetSegment.model_validate(item) for item in manifest["segments"]),
        key=lambda segment: segment.segment_index,
    )
    if not segments or [s.segment_index for s in segments] != list(range(len(segments))):
        raise IntegrityError("manifest is not a complete ordered segment list")
    if len({s.asset_id for s in segments}) != 1:
        raise IntegrityError("manifest mixes remote assets")
    return segments


def _join(segments: list[RemoteAssetSegment], cloud: Path, destination: Path, expected: dict, root: Path) -> None:
    whole = MediaFacts.model_validate(expected)
    defect = timeline_defect(
        [(s.segment_index, s.start_seconds, s.end_seconds) for s in segments],
        whole.duration_seconds,
        tolerance=PreparationPolicy().duration_tolerance,
    )
    if defect:
        raise IntegrityError(defect)
    require_space([(root, int(sum(s.size_bytes for s in segments) * 1.05) + 16 * 1024**2)])
    with retained_temporary(prefix="join-", directory=root) as temporary:
        partial = temporary / "complete.mp4"
        PartMedia().merge(
            [contained_file(cloud, cloud / s.filename) for s in segments],
            partial,
            whole.duration_seconds,
            spans=[s.end_seconds - s.start_seconds for s in segments],
        )
        with partial.open("rb") as handle:
            os.fsync(handle.fileno())
        os.link(partial, destination)


def materialize(manifest: dict, root: Path) -> dict:
    """Fetch every segment, assemble the object, and describe what was built."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("materialization root must be a real directory")
    segments = _segments(manifest)
    authentication = manifest.get("authentication")
    if authentication is not None and (
        any(s.local_path for s in segments)
        or any(p.name != "cloud" or not p.is_dir() or p.is_symlink() or any(p.iterdir()) for p in root.iterdir())
    ):
        raise ValueError("authenticated playback requires fresh inputs")
    output = str(manifest["output"])
    if not _OUTPUT_NAME.fullmatch(output):
        raise ValueError("output must be named by the playable asset identity")
    cloud = root / "cloud"
    cloud.mkdir(mode=0o700, exist_ok=True)
    if cloud.is_symlink():
        raise ValueError("cloud root is a symlink")
    destination = root / output
    if destination.exists() or destination.is_symlink():
        raise ValueError("materialization requires a fresh work directory")
    reports = [
        (
            download_original(segment, cloud, authentication=authentication)
            if authentication is not None
            else download_original(segment, cloud)
        )
        for segment in segments
    ]
    if len(segments) == 1:
        os.link(contained_file(cloud, cloud / segments[0].filename), destination)
        # The same inode the read-back just observed and hashed in full.
        observed = reports[0]["observed"]
        junctions = "single-segment"
    else:
        _join(segments, cloud, destination, manifest.get("expected") or {}, root)
        observed = observed_media(destination)
        junctions = "PASS"
    contained_file(root, destination)
    receipt = {
        "method": MATERIALIZATION_METHOD,
        "segments": [
            {
                "index": s.segment_index,
                "size": r["size"],
                "sha256": r["sha256"],
                "method": r["method"],
                **{
                    key: r[key]
                    for key in ("receipt_version", "authentication", "cold_inputs", "cold_mounts", "decode")
                    if key in r
                },
            }
            for s, r in zip(segments, reports, strict=True)
        ],
        "junctions": junctions,
        "observed": observed,
        "filename": output,
        "size": observed["size_bytes"],
        "sha256": observed["sha256"],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    # The process checks its own work before reporting it: a receipt that
    # cannot pass must never leave the container looking like a success.
    validate_materialization(manifest.get("expected") or {}, segments, receipt)
    return receipt


def main() -> int:
    os.umask(0o077)
    try:
        print(json.dumps(materialize(json.load(sys.stdin), WORK_ROOT)))
        return 0
    except Exception as exc:  # noqa: BLE001 - the type is the only safe detail
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
