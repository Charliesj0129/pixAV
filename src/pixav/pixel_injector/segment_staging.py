"""Present one prepared segment to the upload guest under its own identity.

The guest addresses a segment by a canonical name that carries the asset id,
the segment index and a hash prefix, so a file from another manifest version can
never be mistaken for this one inside the media store. The prepared artifact
itself keeps its own path and is never moved or renamed: staging adds a link,
and only the link is ever removed here. Deleting the artifact is the cleanup
gate's decision alone.
"""

from __future__ import annotations

import errno
import os
import shutil
import threading
from contextlib import ExitStack
from pathlib import Path

from pixav.shared.exceptions import UploadError
from pixav.shared.media_file import MediaDirectory, MediaFile, file_version, open_media_directory, open_media_file
from pixav.shared.storage_models import RemoteAssetSegment


class SegmentStagingError(UploadError):
    """Local staging conflict before any remote upload effect is started."""


def _reject_symlinks(path: Path) -> None:
    """Refuse a path whose own name or any parent is a symlink.

    A link anywhere on the way to the staging entry would let something outside
    the staging root decide which bytes the guest receives.
    """
    if path.is_symlink():
        raise SegmentStagingError("symlink in the segment staging path")
    for parent in path.parents:
        if parent.is_symlink():
            raise SegmentStagingError("symlink in the segment staging path")


def segment_directory(segment: RemoteAssetSegment, *, root: Path) -> Path:
    """Where this asset's segments are presented, one directory per asset."""
    return root / str(segment.asset_id)


def _check_stopped(stopped: threading.Event | None) -> None:
    if stopped is not None and stopped.is_set():
        raise SegmentStagingError("SEGMENT_STAGING_CANCELLED")


def _expected(source: MediaFile, segment: RemoteAssetSegment) -> None:
    if os.fstat(source.descriptor).st_size != segment.size_bytes or source.facts() != (
        segment.sha256,
        segment.size_bytes,
    ):
        raise SegmentStagingError("SEGMENT_STAGING_CONTENT_MISMATCH")


def _copy_segment(
    source: MediaFile,
    target: Path,
    directory: MediaDirectory,
    segment: RemoteAssetSegment,
    stopped: threading.Event | None,
) -> None:
    """Keep interrupted or foreign pending work; publish without replacing a name."""
    pending = target.with_name(target.name + ".pending")
    before = file_version(os.fstat(source.descriptor))
    with open_media_file(pending, exclusive=True, writable=True, create=True, directory=directory) as output:
        with source.reader() as reader, os.fdopen(os.dup(output.descriptor), "wb") as writer:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        _check_stopped(stopped)
        source.assert_current()
        if before != file_version(os.fstat(source.descriptor)):
            raise SegmentStagingError("SEGMENT_STAGING_SOURCE_CHANGED")
        _expected(source, segment)
        _expected(output, segment)
        output.assert_current()
        directory.assert_current()
        _check_stopped(stopped)
        os.link(
            pending.name,
            target.name,
            src_dir_fd=directory.descriptor,
            dst_dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
        # The new name must still identify the inode held by this copy worker.
        MediaFile(target, output.descriptor, directory.descriptor, True).assert_current()
        output.assert_current()
        os.unlink(pending.name, dir_fd=directory.descriptor)
        os.fsync(directory.descriptor)


def stage_segment(segment: RemoteAssetSegment, *, root: Path, stopped: threading.Event | None = None) -> Path:
    """Link the prepared bytes into the staging root under the canonical name.

    Returns the per-asset directory. The link shares the artifact's inode, so no
    second copy of a multi-gigabyte film exists while an upload runs, and the
    uploader's own name, size and SHA-256 checks still describe the real bytes.
    """
    source_path = Path(os.path.abspath(segment.local_path))
    directory = segment_directory(segment, root=Path(os.path.abspath(root)))
    target = directory / segment.filename
    try:
        _check_stopped(stopped)
        _reject_symlinks(source_path)
        _reject_symlinks(directory)
        with open_media_file(source_path) as source:
            _expected(source, segment)
            _check_stopped(stopped)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open_media_directory(target, exclusive=True) as parent:
                if target.exists() or target.is_symlink():
                    with open_media_file(target, directory=parent) as existing:
                        _expected(existing, segment)
                    return directory
                source.assert_current()
                _check_stopped(stopped)
                try:
                    os.link(
                        source_path.name,
                        target.name,
                        src_dir_fd=source.parent_descriptor,
                        dst_dir_fd=parent.descriptor,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    if error.errno != errno.EXDEV:
                        raise
                    _copy_segment(source, target, parent, segment, stopped)
                else:
                    MediaFile(target, source.descriptor, parent.descriptor, False).assert_current()
                    _check_stopped(stopped)
                    _expected(source, segment)
                    os.fsync(parent.descriptor)
                _check_stopped(stopped)
                return directory
    except UploadError:
        raise
    except (OSError, ValueError):
        raise SegmentStagingError("SEGMENT_STAGING_REQUIRES_RECONCILIATION") from None


def _remove_empty_directory(directory: Path, identity: tuple[int, int]) -> None:
    try:
        with open_media_directory(directory, exclusive=True) as parent:
            current = os.stat(directory.name, dir_fd=parent.descriptor, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == identity:
                os.rmdir(directory.name, dir_fd=parent.descriptor)
                os.fsync(parent.descriptor)
    except (OSError, ValueError):
        pass


def release_segment(segment: RemoteAssetSegment, *, root: Path, stopped: threading.Event | None = None) -> bool:
    """Drop the staged link for a segment whose usage is already committed.

    Returns whether a link was removed. The prepared artifact is never touched:
    only the cleanup gate may decide that local media can go.
    """
    if segment.usage_counted_at is None:
        raise UploadError("a segment without committed usage must stay staged")
    directory = segment_directory(segment, root=Path(os.path.abspath(root)))
    target = directory / segment.filename
    source_path = Path(os.path.abspath(segment.local_path))
    if source_path == target:
        return False
    try:
        _check_stopped(stopped)
        # An exclusive source lock protects the prepared copy through unlink.
        # For a hardlink it also protects every reader of the staged inode.
        with ExitStack() as files:
            source = files.enter_context(open_media_file(source_path, exclusive=True))
            parent = files.enter_context(open_media_directory(target, exclusive=True))
            current = os.stat(target.name, dir_fd=parent.descriptor, follow_symlinks=False)
            original = os.fstat(source.descriptor)
            if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                staged = MediaFile(target, source.descriptor, parent.descriptor, True)
            else:
                staged = files.enter_context(open_media_file(target, exclusive=True, directory=parent))
            _expected(source, segment)
            if staged.descriptor != source.descriptor:
                _expected(staged, segment)
            source.assert_current()
            staged.assert_current()
            parent.assert_current()
            _check_stopped(stopped)
            info = os.fstat(parent.descriptor)
            os.unlink(target.name, dir_fd=parent.descriptor)
            os.fsync(parent.descriptor)
        _check_stopped(stopped)
        _remove_empty_directory(directory, (info.st_dev, info.st_ino))
        return True
    except (OSError, ValueError, UploadError):
        return False
