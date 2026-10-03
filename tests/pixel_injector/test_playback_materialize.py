"""Isolated materialization entry point (BDD-082, 083, 084).

Photos retrieval and FFmpeg are replaced at the module boundary; ordering,
naming, junction checks and the self-check of the receipt run for real.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
from pathlib import Path

import pytest

from pixav.pixel_injector import playback_materialize
from pixav.shared.storage_models import IntegrityError

ASSET = uuid.uuid4()
OUTPUT = f"{uuid.uuid4()}.mp4"
STREAMS = [
    {"kind": "video", "codec": "h264", "width": 1920, "height": 1080},
    {"kind": "audio", "codec": "aac", "width": 0, "height": 0},
]


def facts(content: bytes, duration: float = 20.0) -> dict:
    return {
        "container": "mov,mp4",
        "size_bytes": len(content),
        "duration_seconds": duration,
        "sha256": hashlib.sha256(content).hexdigest(),
        "streams": STREAMS,
    }


def segment(index: int, start: float, end: float, content: bytes) -> dict:
    return {
        "asset_id": str(ASSET),
        "segment_index": index,
        "start_seconds": start,
        "end_seconds": end,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "local_path": "",
        "share_url": "https://photos.app.goo.gl/synthetic",
    }


@pytest.fixture
def photos(monkeypatch):
    """Serve each segment's bytes as the provider original would."""
    served: dict[str, bytes] = {}

    def download(part, root: Path) -> dict:
        content = served[part.sha256]
        (root / part.filename).write_bytes(content)
        return {
            "method": "photos-original-browser",
            "size": len(content),
            "sha256": part.sha256,
            "observed": facts(content),
        }

    monkeypatch.setattr(playback_materialize, "download_original", download)
    return served


def test_single_segment_object_is_the_original_bdd_066(tmp_path, photos):
    content = b"one whole synthetic film"
    photos[hashlib.sha256(content).hexdigest()] = content
    manifest = {"segments": [segment(0, 0.0, 20.0, content)], "expected": facts(content), "output": OUTPUT}

    receipt = playback_materialize.materialize(manifest, tmp_path)

    assert receipt["junctions"] == "single-segment" and receipt["sha256"] == hashlib.sha256(content).hexdigest()
    assert (tmp_path / OUTPUT).read_bytes() == content


def test_segments_are_joined_in_order_bdd_083(tmp_path, photos, monkeypatch):
    first, second = b"first half", b"second half"
    joined = first + second
    for content in (first, second):
        photos[hashlib.sha256(content).hexdigest()] = content
    calls = []

    def merge(self, paths, destination, duration, *, spans=None):
        calls.append(([p.name for p in paths], spans))
        destination.write_bytes(b"".join(p.read_bytes() for p in paths))

    monkeypatch.setattr(playback_materialize.PartMedia, "merge", merge)
    monkeypatch.setattr(playback_materialize, "require_space", lambda requirements: None)
    monkeypatch.setattr(playback_materialize, "observed_media", lambda path: facts(path.read_bytes()))
    manifest = {
        "segments": [segment(1, 10.0, 20.0, second), segment(0, 0.0, 10.0, first)],
        "expected": facts(b"x" * 40),
        "output": OUTPUT,
    }

    receipt = playback_materialize.materialize(manifest, tmp_path)

    assert receipt["junctions"] == "PASS" and (tmp_path / OUTPUT).read_bytes() == joined
    assert calls[0][1] == [10.0, 10.0] and calls[0][0][0].endswith(
        "part-0-" + receipt["segments"][0]["sha256"][:16] + ".mp4"
    )


def test_timeline_gap_is_refused_before_joining_bdd_084(tmp_path, photos, monkeypatch):
    first, second = b"first half", b"second half"
    for content in (first, second):
        photos[hashlib.sha256(content).hexdigest()] = content
    monkeypatch.setattr(playback_materialize.PartMedia, "merge", lambda *args, **kwargs: pytest.fail("joined"))
    manifest = {
        "segments": [segment(0, 0.0, 9.0, first), segment(1, 10.0, 20.0, second)],
        "expected": facts(b"x" * 40),
        "output": OUTPUT,
    }

    with pytest.raises(IntegrityError, match="gap or overlap"):
        playback_materialize.materialize(manifest, tmp_path)
    assert not (tmp_path / OUTPUT).exists()


def test_incomplete_manifest_is_refused(tmp_path, photos):
    content = b"second half"
    manifest = {"segments": [segment(1, 10.0, 20.0, content)], "expected": facts(content), "output": OUTPUT}

    with pytest.raises(IntegrityError, match="complete ordered"):
        playback_materialize.materialize(manifest, tmp_path)


@pytest.mark.parametrize("output", ["../escape.mp4", "title - name.mp4", "prefix-" + OUTPUT])
def test_output_must_be_named_by_identity(tmp_path, photos, output):
    content = b"one whole synthetic film"
    manifest = {"segments": [segment(0, 0.0, 20.0, content)], "expected": facts(content), "output": output}

    with pytest.raises(ValueError, match="identity"):
        playback_materialize.materialize(manifest, tmp_path)


def test_existing_output_means_the_directory_is_not_fresh(tmp_path, photos):
    content = b"one whole synthetic film"
    photos[hashlib.sha256(content).hexdigest()] = content
    (tmp_path / OUTPUT).write_bytes(b"left behind")
    manifest = {"segments": [segment(0, 0.0, 20.0, content)], "expected": facts(content), "output": OUTPUT}

    with pytest.raises(ValueError, match="fresh"):
        playback_materialize.materialize(manifest, tmp_path)


def test_failure_reports_only_the_exception_type(monkeypatch, capsys):
    monkeypatch.setattr(playback_materialize, "WORK_ROOT", Path("/nonexistent-work-root"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"segments": []})))

    assert playback_materialize.main() == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"status": "BLOCKED", "error_type": "ValueError"}
