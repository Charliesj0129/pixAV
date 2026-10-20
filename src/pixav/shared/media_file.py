"""Linux media reader/writer locks bound to a directory and an existing inode.

No lock files are created. Readers duplicate the locked descriptor or pass it
to their child process; closing the last duplicate releases the kernel lock.
All pixAV publishers and deleters must acquire the exclusive form before
replacing an existing name. This is an advisory protocol on the accepted local
filesystem, not protection against arbitrary processes that ignore the lock.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO


class MediaFileChangedError(ValueError):
    """A media name, parent or content no longer identifies the opened file."""


class MediaFileUnsafeError(ValueError):
    """A target is not an owned ordinary media file."""


async def run_media_work(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Drain a started file thread before propagating even repeated cancellation.

    The operation checks ``stopped`` before its next publication or mutation.
    A completed mutation is retained for reconciliation by the caller.
    """
    stopped = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs, stopped=stopped))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stopped.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except (asyncio.CancelledError, Exception):
                if task.done():
                    break
        with suppress(asyncio.CancelledError, Exception):
            task.result()
        raise


def file_version(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def descriptor_facts(descriptor: int) -> tuple[str, int]:
    """Hash the locked object without reopening its name or moving its offset."""
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise MediaFileUnsafeError("MEDIA_FILE_NOT_REGULAR")
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(descriptor, 1024 * 1024, offset):
        digest.update(chunk)
        offset += len(chunk)
    if file_version(os.fstat(descriptor)) != file_version(before) or offset != before.st_size:
        raise MediaFileChangedError("MEDIA_FILE_CHANGED_WHILE_HASHING")
    return digest.hexdigest(), offset


def _parent_descriptor(path: Path) -> int:
    """Walk absolute directory components without following any symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:-1]:
            following = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@dataclass(frozen=True)
class MediaDirectory:
    path: Path
    descriptor: int
    exclusive: bool

    def assert_current(self) -> None:
        current = _parent_descriptor(self.path)
        try:
            before, after = os.fstat(self.descriptor), os.fstat(current)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise MediaFileChangedError("MEDIA_PARENT_CHANGED")
        finally:
            os.close(current)


@contextmanager
def open_media_directory(path: Path | str, *, exclusive: bool = False) -> Iterator[MediaDirectory]:
    """Coordinate brief opens/name mutations, including a currently absent name."""
    absolute = Path(os.path.abspath(path))
    parent = _parent_descriptor(absolute)
    try:
        fcntl.flock(parent, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        guard = MediaDirectory(absolute, parent, exclusive)
        guard.assert_current()
        yield guard
    finally:
        os.close(parent)


@dataclass(frozen=True)
class MediaFile:
    path: Path
    descriptor: int
    parent_descriptor: int
    exclusive: bool

    def assert_current(self) -> None:
        parent = _parent_descriptor(self.path)
        try:
            expected, current = os.fstat(self.parent_descriptor), os.fstat(parent)
            if (expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino):
                raise MediaFileChangedError("MEDIA_PARENT_CHANGED")
        finally:
            os.close(parent)
        named = os.stat(self.path.name, dir_fd=self.parent_descriptor, follow_symlinks=False)
        opened = os.fstat(self.descriptor)
        if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise MediaFileChangedError("MEDIA_NAME_CHANGED")

    def reader(self) -> BinaryIO:
        self.assert_current()
        return os.fdopen(os.dup(self.descriptor), "rb", buffering=0)

    def facts(self) -> tuple[str, int]:
        self.assert_current()
        result = descriptor_facts(self.descriptor)
        self.assert_current()
        return result

    def unlink(self) -> None:
        if not self.exclusive:
            raise MediaFileUnsafeError("MEDIA_EXCLUSIVE_LOCK_REQUIRED")
        with open_media_directory(self.path, exclusive=True) as parent:
            self.assert_current()
            os.unlink(self.path.name, dir_fd=parent.descriptor)
            os.fsync(parent.descriptor)


@contextmanager
def open_media_file(
    path: Path | str,
    *,
    exclusive: bool = False,
    directory: MediaDirectory | None = None,
    writable: bool = False,
    create: bool = False,
) -> Iterator[MediaFile]:
    """Lock an existing owned file without waiting, following links or updating atime."""
    absolute = Path(os.path.abspath(path))
    if (writable or create) and not exclusive:
        raise MediaFileUnsafeError("MEDIA_WRITER_REQUIRES_EXCLUSIVE_LOCK")
    if create and (directory is None or not directory.exclusive):
        raise MediaFileUnsafeError("MEDIA_CREATE_REQUIRES_DIRECTORY_LOCK")
    if directory is not None:
        directory.assert_current()
    parent = _parent_descriptor(absolute)
    descriptor = None
    try:
        if directory is not None:
            expected, current = os.fstat(directory.descriptor), os.fstat(parent)
            if (expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino):
                raise MediaFileUnsafeError("MEDIA_DIRECTORY_LOCK_MISMATCH")
        if directory is None:
            fcntl.flock(parent, fcntl.LOCK_SH | fcntl.LOCK_NB)
        descriptor = os.open(
            absolute.name,
            (os.O_RDWR if writable else os.O_RDONLY)
            | (os.O_CREAT | os.O_EXCL if create else 0)
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
            | os.O_NONBLOCK
            | os.O_NOATIME,
            0o600,
            dir_fd=parent,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise MediaFileUnsafeError("MEDIA_OWNED_REGULAR_FILE_REQUIRED")
        fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        opened = MediaFile(absolute, descriptor, parent, exclusive)
        opened.assert_current()
        if directory is None:
            # Opens serialize with name mutations only for this short handoff.
            # The file lock, not a directory lock, protects a long reader.
            fcntl.flock(parent, fcntl.LOCK_UN)
        yield opened
    finally:
        # Do not LOCK_UN: an inherited/duplicated reader must retain its lock.
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


@contextmanager
def open_media_output(path: Path | str, *, must_create: bool = False) -> Iterator[MediaFile]:
    """Create or open a caller-owned output without truncating it before its lock."""
    with ExitStack() as resources:
        with open_media_directory(path, exclusive=True) as directory:
            if must_create:
                output = resources.enter_context(
                    open_media_file(path, exclusive=True, writable=True, create=True, directory=directory)
                )
            else:
                try:
                    output = resources.enter_context(
                        open_media_file(path, exclusive=True, writable=True, directory=directory)
                    )
                except FileNotFoundError:
                    output = resources.enter_context(
                        open_media_file(path, exclusive=True, writable=True, create=True, directory=directory)
                    )
        yield output
