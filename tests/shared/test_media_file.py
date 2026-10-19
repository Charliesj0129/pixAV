"""Real Linux descriptor locks, name races and child-process ownership."""

from __future__ import annotations

import asyncio
import hashlib
import os
import select
import subprocess
import sys
import threading

import pytest

from pixav.maxwell_core import gc
from pixav.shared.media_file import (
    MediaFileChangedError,
    MediaFileUnsafeError,
    open_media_directory,
    open_media_file,
    open_media_output,
)


def test_inspection_changes_neither_atime_nor_directory_entries(tmp_path):
    path = tmp_path / "synthetic.mp4"
    content = b"synthetic media"
    path.write_bytes(content)
    os.utime(path, ns=(1_000_000_000, 2_000_000_000))
    before = path.stat()
    with open_media_file(path) as source:
        assert source.facts() == (hashlib.sha256(content).hexdigest(), len(content))
        with source.reader() as reader:
            assert reader.read() == content
    after = path.stat()
    assert (after.st_atime_ns, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_atime_ns,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    assert list(tmp_path.iterdir()) == [path]


def test_duplicated_reader_keeps_lock_after_guard_closes(tmp_path):
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"synthetic")
    with open_media_file(path) as source:
        reader = source.reader()
    try:
        with pytest.raises(BlockingIOError), open_media_file(path, exclusive=True):
            pytest.fail("a live duplicate must block deletion")
        assert reader.read() == b"synthetic"
    finally:
        reader.close()
    with open_media_file(path, exclusive=True) as writer:
        writer.unlink()
    assert not path.exists()


def test_child_inherits_lock_until_its_last_descriptor_closes(tmp_path):
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"synthetic")
    code = """
import os, sys
assert os.pread(int(sys.argv[1]), 9, 0) == b'synthetic'
print('ready', flush=True)
sys.stdin.buffer.read(1)
"""
    with open_media_file(path) as source:
        process = subprocess.Popen(  # noqa: S603 -- fixed child, synthetic file descriptor only
            [sys.executable, "-c", code, str(source.descriptor)],
            pass_fds=(source.descriptor,),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    try:
        assert select.select([process.stdout], [], [], 5)[0], "child did not start"
        assert process.stdout.readline() == b"ready\n"
        with pytest.raises(BlockingIOError), open_media_file(path, exclusive=True):
            pytest.fail("the child still owns the inherited read lock")
        process.communicate(b"x", timeout=5)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    with open_media_file(path, exclusive=True) as writer:
        writer.unlink()


@pytest.mark.parametrize("replacement", ["name", "parent"])
def test_replaced_name_or_parent_cannot_be_unlinked(tmp_path, replacement):
    directory = tmp_path / "objects"
    directory.mkdir()
    path = directory / "synthetic.mp4"
    path.write_bytes(b"original")
    with open_media_file(path, exclusive=True) as writer:
        if replacement == "name":
            path.rename(directory / "retained.mp4")
        else:
            directory.rename(tmp_path / "retained")
            directory.mkdir()
        path.write_bytes(b"replacement")
        with pytest.raises(MediaFileChangedError):
            writer.unlink()
    assert path.read_bytes() == b"replacement"
    retained = directory / "retained.mp4" if replacement == "name" else tmp_path / "retained/synthetic.mp4"
    assert retained.read_bytes() == b"original"


@pytest.mark.parametrize("kind", ["file-link", "parent-link", "fifo"])
def test_symlinks_and_nonregular_files_are_refused(tmp_path, kind):
    path = tmp_path / "synthetic.mp4"
    if kind == "file-link":
        path.symlink_to(tmp_path / "missing")
    elif kind == "parent-link":
        (tmp_path / "objects").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "objects", target_is_directory=True)
        path = tmp_path / "link/synthetic.mp4"
    else:
        os.mkfifo(path)
    with pytest.raises((OSError, MediaFileUnsafeError)), open_media_file(path):
        pytest.fail("unsafe input accepted")


def test_output_does_not_truncate_a_live_reader_and_releases_directory_lock(tmp_path):
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"original")
    with open_media_file(path), pytest.raises(BlockingIOError), open_media_output(path):
        pytest.fail("live input must block the writer")
    assert path.read_bytes() == b"original"
    with open_media_output(path) as output:
        # A long writer owns its inode, not all unrelated files in its directory.
        with open_media_directory(tmp_path / "unrelated", exclusive=True):
            pass
        with pytest.raises(BlockingIOError), open_media_file(path):
            pytest.fail("an active writer must block new readers")
        os.pwrite(output.descriptor, b"updated!", 0)
    assert path.read_bytes() == b"updated!"


def test_fresh_output_is_private_and_never_overwrites_an_existing_name(tmp_path):
    path = tmp_path / "synthetic.mp4"
    with open_media_output(path, must_create=True) as output:
        assert path.stat().st_mode & 0o777 == 0o600
        os.write(output.descriptor, b"synthetic")
    with pytest.raises(FileExistsError), open_media_output(path, must_create=True):
        pytest.fail("a fresh output cannot reuse an existing name")
    assert path.read_bytes() == b"synthetic"


def test_directory_guard_must_protect_the_same_parent(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    with open_media_directory(tmp_path / "first", exclusive=True) as directory:
        with (
            pytest.raises(MediaFileUnsafeError, match="MISMATCH"),
            open_media_file(other / "second", exclusive=True, writable=True, create=True, directory=directory),
        ):
            pytest.fail("unrelated directory lock cannot authorize create")
    assert not (other / "second").exists()


def test_directory_mutation_blocks_reader_handoff(tmp_path):
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"synthetic")
    with open_media_directory(path, exclusive=True):
        with pytest.raises(BlockingIOError), open_media_file(path):
            pytest.fail("reader cannot race an active name mutation")


async def test_legacy_local_stream_blocks_inode_deletion_until_stream_closes(tmp_path):
    from pixav.strm_resolver.routes import _stream_file

    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"synthetic")
    stream = _stream_file(str(path), start=0, end=8)
    try:
        assert await anext(stream) == b"synthetic"
        with pytest.raises(BlockingIOError), open_media_file(path, exclusive=True):
            pytest.fail("the local route is still serving this inode")
    finally:
        await stream.aclose()
    with open_media_file(path, exclusive=True):
        pass


def test_complete_original_decoder_cannot_open_an_active_writer(tmp_path, monkeypatch):
    from pixav.pixel_injector.parts_download import decode_original

    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"synthetic")
    called = []

    def completed(*args, **kwargs):
        called.append(True)
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(subprocess, "run", completed)
    with open_media_file(path, exclusive=True):
        with pytest.raises(BlockingIOError):
            decode_original(path)
    assert not called


async def test_cancelled_hash_keeps_its_duplicate_locked_until_thread_finishes(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"synthetic")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = gc.artifact_facts

    def slow_hash(path, descriptor=None):
        started.set()
        assert release.wait(timeout=5)
        try:
            return original(path, descriptor)
        finally:
            finished.set()

    monkeypatch.setattr(gc, "artifact_facts", slow_hash)
    try:
        with open_media_file(path, exclusive=True) as source:
            task = asyncio.create_task(gc._artifact_facts_async(path, source.descriptor))
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        with pytest.raises(BlockingIOError), open_media_file(path, exclusive=True):
            pytest.fail("cancelling the await cannot release the hash worker's lock")
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    for _ in range(100):
        try:
            with open_media_file(path, exclusive=True):
                return
        except BlockingIOError:
            await asyncio.sleep(0.01)
    pytest.fail("hash worker leaked its duplicated descriptor")
