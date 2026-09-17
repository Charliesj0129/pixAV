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
import logging
import os
import shutil
from pathlib import Path

from pixav.shared.exceptions import UploadError
from pixav.shared.storage_models import RemoteAssetSegment

logger = logging.getLogger(__name__)


def _reject_symlinks(path: Path) -> None:
    """Refuse a path whose own name or any parent is a symlink.

    A link anywhere on the way to the staging entry would let something outside
    the staging root decide which bytes the guest receives.
    """
    if path.is_symlink():
        raise UploadError("symlink in the segment staging path")
    for parent in path.parents:
        if parent.is_symlink():
            raise UploadError("symlink in the segment staging path")


def segment_directory(segment: RemoteAssetSegment, *, root: Path) -> Path:
    """Where this asset's segments are presented, one directory per asset."""
    return root / str(segment.asset_id)


def stage_segment(segment: RemoteAssetSegment, *, root: Path) -> Path:
    """Link the prepared bytes into the staging root under the canonical name.

    Returns the per-asset directory. The link shares the artifact's inode, so no
    second copy of a multi-gigabyte film exists while an upload runs, and the
    uploader's own name, size and SHA-256 checks still describe the real bytes.
    """
    source = Path(segment.local_path)
    _reject_symlinks(source)
    if not source.is_file():
        raise UploadError("prepared segment is missing from local staging")
    if source.stat().st_size != segment.size_bytes:
        raise UploadError("prepared segment size does not match the recorded fact")

    directory = segment_directory(segment, root=root)
    _reject_symlinks(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / segment.filename
    _reject_symlinks(target)

    if target.exists():
        existing, original = target.stat(), source.stat()
        if existing.st_size != segment.size_bytes:
            # Someone else's bytes under our name. Overwriting would hide it.
            raise UploadError("a different file already occupies the staged segment name")
        if (existing.st_dev, existing.st_ino) != (original.st_dev, original.st_ino):
            logger.info("reusing an existing staged copy of segment %s", segment.segment_index)
        return directory

    try:
        os.link(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise UploadError("cannot stage the prepared segment for upload") from exc
        # The staging root is on another filesystem, so a link is impossible.
        # Copying costs the segment's size on disk but keeps the artifact intact.
        logger.info("staging root is on another filesystem; copying segment %s", segment.segment_index)
        pending = target.with_name(target.name + ".pending")
        shutil.copyfile(source, pending)
        os.replace(pending, target)
    if target.stat().st_size != segment.size_bytes:
        raise UploadError("staged segment size does not match the recorded fact")
    return directory


def release_segment(segment: RemoteAssetSegment, *, root: Path) -> bool:
    """Drop the staged link for a segment whose usage is already committed.

    Returns whether a link was removed. The prepared artifact is never touched:
    only the cleanup gate may decide that local media can go.
    """
    if segment.usage_counted_at is None:
        raise UploadError("a segment without committed usage must stay staged")
    directory = segment_directory(segment, root=root)
    target = directory / segment.filename
    if target.is_symlink() or not target.exists():
        return False
    if target.resolve() == Path(segment.local_path).resolve():
        # A same-filesystem link was never made, or the artifact itself sits in
        # the staging root. Unlinking here would destroy recoverable local media.
        return False
    target.unlink()
    try:
        directory.rmdir()
    except OSError:
        # Other segments of the same asset are still staged.
        pass
    return True
