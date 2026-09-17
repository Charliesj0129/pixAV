"""Presenting a prepared segment to the upload guest without endangering it."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest

from pixav.pixel_injector.segment_staging import release_segment, segment_directory, stage_segment
from pixav.shared.exceptions import UploadError
from pixav.shared.storage_models import RemoteAssetSegment

PAYLOAD = b"managed segment bytes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def artifact(tmp_path, payload: bytes = PAYLOAD):
    path = tmp_path / "prepared" / "movie.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def segment(path, **changes) -> RemoteAssetSegment:
    fields = {
        "asset_id": uuid.UUID("11111111-1111-4111-8111-111111111111"),
        "segment_index": 0,
        "start_seconds": 0.0,
        "end_seconds": 12.0,
        "size_bytes": len(PAYLOAD),
        "sha256": DIGEST,
        "local_path": str(path),
    }
    return RemoteAssetSegment(**{**fields, **changes})


def test_staging_links_the_artifact_under_its_canonical_name(tmp_path):
    source = artifact(tmp_path)
    item = segment(source)

    directory = stage_segment(item, root=tmp_path / "staging")

    staged = directory / item.filename
    assert staged.read_bytes() == PAYLOAD
    assert staged.stat().st_ino == source.stat().st_ino, "a link, not a second copy of the film"
    assert directory == segment_directory(item, root=tmp_path / "staging")


def test_staging_is_idempotent_across_a_retried_attempt(tmp_path):
    source = artifact(tmp_path)
    item = segment(source)
    root = tmp_path / "staging"

    first = stage_segment(item, root=root)
    second = stage_segment(item, root=root)

    assert first == second
    assert list(first.iterdir()) == [first / item.filename]


def test_a_missing_artifact_is_refused_before_any_container_is_asked_for(tmp_path):
    item = segment(tmp_path / "prepared" / "gone.mp4")
    with pytest.raises(UploadError):
        stage_segment(item, root=tmp_path / "staging")


def test_an_artifact_whose_size_contradicts_the_record_is_refused(tmp_path):
    """Uploading these bytes would attach a false size to a durable asset."""
    source = artifact(tmp_path, payload=PAYLOAD + b"more")
    with pytest.raises(UploadError):
        stage_segment(segment(source), root=tmp_path / "staging")


def test_a_symlinked_artifact_is_refused(tmp_path):
    real = artifact(tmp_path)
    link = tmp_path / "prepared" / "link.mp4"
    link.symlink_to(real)
    with pytest.raises(UploadError):
        stage_segment(segment(link), root=tmp_path / "staging")


def test_a_symlinked_staging_root_is_refused(tmp_path):
    source = artifact(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    root = tmp_path / "staging"
    root.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(UploadError):
        stage_segment(segment(source), root=root)


def test_a_foreign_file_under_the_staged_name_is_never_overwritten(tmp_path):
    source = artifact(tmp_path)
    item = segment(source)
    root = tmp_path / "staging"
    directory = segment_directory(item, root=root)
    directory.mkdir(parents=True)
    (directory / item.filename).write_bytes(b"someone else")

    with pytest.raises(UploadError):
        stage_segment(item, root=root)


def test_release_requires_committed_usage(tmp_path):
    """An unconfirmed upload still needs its presentation to reconcile against."""
    source = artifact(tmp_path)
    item = segment(source)
    stage_segment(item, root=tmp_path / "staging")

    with pytest.raises(UploadError):
        release_segment(item, root=tmp_path / "staging")


def test_release_drops_the_link_and_leaves_the_artifact_bdd_004(tmp_path):
    """Only the cleanup gate may decide that local media can go."""
    source = artifact(tmp_path)
    item = segment(source)
    root = tmp_path / "staging"
    directory = stage_segment(item, root=root)
    confirmed = item.model_copy(update={"usage_counted_at": datetime.now(timezone.utc)})

    assert release_segment(confirmed, root=root) is True
    assert not (directory / item.filename).exists()
    assert source.read_bytes() == PAYLOAD
    assert not directory.exists(), "the asset's directory goes once its last segment does"


def test_release_never_unlinks_the_artifact_itself(tmp_path):
    """If the artifact already sits at the staged path, releasing must do nothing."""
    item = segment(tmp_path / "x")
    root = tmp_path / "staging"
    directory = segment_directory(item, root=root)
    directory.mkdir(parents=True)
    staged = directory / item.filename
    staged.write_bytes(PAYLOAD)
    confirmed = segment(staged).model_copy(update={"usage_counted_at": datetime.now(timezone.utc)})

    assert release_segment(confirmed, root=root) is False
    assert staged.exists()
