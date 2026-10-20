"""Atomic projection publication (BDD-101, 102, 103, 104, 106, 107).

Layout under the library root::

    library/pixav-<video_id>                -> ../versions/<video_id>/<n>-<digest12>
    versions/<video_id>/<n>-<digest12>/     complete, fsynced, verified files

The media server scans ``library/``. A version directory is written under a
temporary name, every file is fsynced and read back against the spec, and only
then is it renamed into place; the item link is swapped with one ``rename``.
A reader therefore sees the old item or the new item, never a mixture. The
previous version is kept so a reader already inside it can finish. Only links
and directories that follow this naming are ever changed or removed; anything
else found at an item's name is refused, not deleted.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import stat
import uuid
from pathlib import Path

from pixav.library_projection.artwork import ArtworkStore
from pixav.library_projection.readonly_cleanup import verify_in_readonly_namespace
from pixav.library_projection.render import ProjectionFile, ProjectionSpec, item_name, projection_digest
from pixav.shared.media_file import file_version, open_media_directory, open_media_file

LIBRARY = "library"
VERSIONS = "versions"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_VERSION_NAME = re.compile(r"(\d{1,9})-([a-f0-9]{12})")
_ITEM_NAME = re.compile(rf"pixav-({_UUID})")
_VIDEO_DIRECTORY = re.compile(_UUID)
_PARTIAL_NAME = re.compile(r"\.(\d{1,9})-([a-f0-9]{12})\.partial-[0-9a-f]{16}")


class ForeignContentError(Exception):
    """Something pixAV did not create occupies an item's name."""


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_file(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _file_digest(path: Path) -> tuple[int, str] | None:
    try:
        with open_media_file(path) as source:
            digest, size = source.facts()
            return size, digest
    except (OSError, ValueError):
        return None


class ProjectionWriter:
    def __init__(self, root: Path, artwork: ArtworkStore) -> None:
        self.library = root / LIBRARY
        self.versions = root / VERSIONS
        self.artwork = artwork

    def _link(self, video_id: uuid.UUID) -> Path:
        return self.library / item_name(video_id)

    def _video_versions(self, video_id: uuid.UUID) -> Path:
        return self.versions / str(video_id)

    def active_version(self, video_id: uuid.UUID) -> str | None:
        """The version the item link points at, or None when there is no intact item.

        Raises ForeignContentError when the name is taken by something else.
        """
        link = self._link(video_id)
        if not link.is_symlink():
            if os.path.lexists(link):
                raise ForeignContentError(item_name(video_id))
            return None
        target = os.readlink(link)
        prefix = f"../{VERSIONS}/{video_id}/"
        name = target.removeprefix(prefix)
        if not target.startswith(prefix) or not _VERSION_NAME.fullmatch(name):
            raise ForeignContentError(item_name(video_id))
        directory = self._video_versions(video_id) / name
        if directory.is_symlink() or not directory.is_dir():
            return None
        return name

    def matches(self, video_id: uuid.UUID, version: str, spec: ProjectionSpec) -> bool:
        """Whether the active item is this spec: linked, and every file present at its size.

        A cheap check for missing or truncated files; ``verify`` reads the bytes.
        """
        if self.active_version(video_id) != version or not version.endswith(spec.digest[:12]):
            return False
        directory = self._video_versions(video_id) / version
        for file in spec.files:
            try:
                facts = os.lstat(directory / file.name)
            except FileNotFoundError:
                return False
            if not stat.S_ISREG(facts.st_mode) or facts.st_size != file.size:
                return False
        return True

    def verify(self, directory: Path, spec: ProjectionSpec) -> bool:
        """Every file of the spec is in the directory with exactly its bytes."""
        return all(_file_digest(directory / file.name) == (file.size, file.sha256) for file in spec.files)

    def publish(self, spec: ProjectionSpec, version_number: int) -> str:
        """Write the spec as a new version, verify it, and make it the active item."""
        video_id = spec.video_id
        # Refuses foreign content before anything is written.
        previous = self.active_version(video_id)
        name = f"{version_number}-{spec.digest[:12]}"
        root = self._video_versions(video_id)
        root.mkdir(parents=True, exist_ok=True)
        final = root / name
        if final.is_symlink() or (final.exists() and not self.verify(final, spec)):
            # Our own leftover under our own name, but not these bytes.
            self._remove_version(final)
        if not final.exists():
            self._write_version(root, name, spec)
        self._swap_link(video_id, name)
        # The version readers may still be inside is kept until the next publish.
        self._prune(video_id, keep={name} | ({previous} if previous else set()))
        return name

    def _write_version(self, root: Path, name: str, spec: ProjectionSpec) -> None:
        partial = root / f".{name}.partial-{secrets.token_hex(8)}"
        partial.mkdir()
        try:
            for file in spec.files:
                content = file.content
                if content is None:
                    content = self.artwork.read_verified(file.sha256, file.size)
                _write_file(partial / file.name, content)
            _fsync_directory(partial)
            if not self.verify(partial, spec):
                raise OSError("projection files do not read back as written")
            os.rename(partial, root / name)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        _fsync_directory(root)

    def _swap_link(self, video_id: uuid.UUID, name: str) -> None:
        self.library.mkdir(parents=True, exist_ok=True)
        for leftover in self.library.glob(f".{item_name(video_id)}.link-*"):
            if leftover.is_symlink():
                leftover.unlink()
        staged = self.library / f".{item_name(video_id)}.link-{secrets.token_hex(8)}"
        os.symlink(f"../{VERSIONS}/{video_id}/{name}", staged)
        try:
            os.replace(staged, self._link(video_id))
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        _fsync_directory(self.library)

    def _prune(self, video_id: uuid.UUID, *, keep: set[str]) -> None:
        root = self._video_versions(video_id)
        for entry in root.iterdir():
            ours = _VERSION_NAME.fullmatch(entry.name) or _PARTIAL_NAME.fullmatch(entry.name)
            if ours and entry.name not in keep:
                self._remove_version(entry)

    @staticmethod
    def _remove_version(path: Path) -> None:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    def withdraw(self, video_id: uuid.UUID) -> bool:
        """Remove the item link and every version of it; return whether anything was there."""
        self.active_version(video_id)  # refuse foreign content rather than removing it
        removed = False
        link = self._link(video_id)
        if link.is_symlink():
            link.unlink()
            _fsync_directory(self.library)
            removed = True
        root = self._video_versions(video_id)
        if root.is_dir() and not root.is_symlink():
            self._prune(video_id, keep=set())
            # Anything left is not ours, so the directory stays with it.
            with contextlib.suppress(OSError):
                root.rmdir()
            removed = True
        return removed

    def items(self) -> set[uuid.UUID]:
        """Every video that has an item link or versions on disk."""
        found: set[uuid.UUID] = set()
        if self.library.is_dir():
            for entry in self.library.iterdir():
                match = _ITEM_NAME.fullmatch(entry.name)
                if match and entry.is_symlink():
                    found.add(uuid.UUID(match.group(1)))
        if self.versions.is_dir():
            for entry in self.versions.iterdir():
                if _VIDEO_DIRECTORY.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink():
                    found.add(uuid.UUID(entry.name))
        return found


def publication_is_complete(root: Path, video_id: uuid.UUID, version: str, digest: str) -> bool:
    """Check the full projection without modifying even symlink/directory atime."""
    root = Path(os.path.abspath(root))
    try:
        with open_media_directory(root / ".pixav-cleanup-root") as directory:
            flags = os.fstatvfs(directory.descriptor).f_flag
            complete = (
                _publication_on_readonly_mount(root, video_id, version, digest)
                if flags & (os.ST_RDONLY | os.ST_NOATIME)
                else verify_in_readonly_namespace(root, video_id, version, digest)
            )
            directory.assert_current()
            return complete
    except (OSError, ValueError):
        return False


def _publication_on_readonly_mount(root: Path, video_id: uuid.UUID, version: str, digest: str) -> bool:
    if not _VERSION_NAME.fullmatch(version) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        return False
    link = root / LIBRARY / item_name(video_id)
    directory = root / VERSIONS / str(video_id) / version
    try:
        with (
            open_media_directory(link) as library,
            open_media_directory(directory / ".pixav-cleanup-version") as committed,
        ):
            # readlink has no O_NOATIME equivalent. Check its actual mount
            # before reading it, including on the already-read-only fast path.
            if not os.fstatvfs(library.descriptor).f_flag & (os.ST_RDONLY | os.ST_NOATIME):
                return False
            before = os.stat(link.name, dir_fd=library.descriptor, follow_symlinks=False)
            if not stat.S_ISLNK(before.st_mode):
                return False
            if os.readlink(link.name, dir_fd=library.descriptor) != f"../{VERSIONS}/{video_id}/{version}":
                return False
            files = []
            listing = os.open(
                ".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NOATIME, dir_fd=committed.descriptor
            )
            try:
                entries = os.listdir(listing)
            finally:
                os.close(listing)
            for name in entries:
                facts = _file_digest(directory / name)
                if facts is None:
                    return False
                size, sha256 = facts
                files.append(ProjectionFile(name=name, size=size, sha256=sha256))
            after = os.stat(link.name, dir_fd=library.descriptor, follow_symlinks=False)
            library.assert_current()
            committed.assert_current()
        if file_version(before) != file_version(after):
            return False
        names = {f.name for f in files}
        required = {f"{item_name(video_id)}.strm", f"{item_name(video_id)}.nfo"}
        return (
            required <= names
            and bool(names & {"poster.png", "poster.jpg", "poster.webp"})
            and projection_digest(files) == digest
        )
    except (OSError, ValueError):
        return False
