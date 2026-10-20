"""Verify original byte validation against real ZIP streams."""

import hashlib
import io
import zipfile

import pytest

from pixav.pixel_injector.canary_download import OriginalWorkConflictError, copy_original, extract_original
from pixav.shared.media_file import open_media_file


def test_complete_original(tmp_path):
    data = b"original bytes" * 1000
    destination = tmp_path / "original.mp4"
    archive = tmp_path / "download.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("canary.mp4", data)
    extract_original(archive, destination, "canary.mp4", len(data), hashlib.sha256(data).hexdigest())
    assert destination.read_bytes() == data
    assert destination.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("data", [b"short", b"different", b"too many bytes"])
def test_reject_corrupt_download_without_publishing(tmp_path, data):
    destination = tmp_path / "original.mp4"
    expected = b"original!"
    with pytest.raises(ValueError):
        copy_original(io.BytesIO(data), destination, len(expected), hashlib.sha256(expected).hexdigest())
    assert not destination.exists()
    assert not destination.with_suffix(".mp4.part").exists()


@pytest.mark.parametrize("names", [["wrong.mp4"], ["canary.mp4", "other.mp4"], ["../canary.mp4"], []])
def test_reject_wrong_archive_item(tmp_path, names):
    archive = tmp_path / "download.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name in names:
            bundle.writestr(name, b"original")
    with pytest.raises(ValueError, match="expected item"):
        extract_original(archive, tmp_path / "out.mp4", "canary.mp4", 8, hashlib.sha256(b"original").hexdigest())


def test_preserve_existing_destination(tmp_path):
    destination = tmp_path / "original.mp4"
    destination.write_bytes(b"existing")
    with pytest.raises(ValueError, match="already exists"):
        copy_original(io.BytesIO(b"replacement"), destination, 11, hashlib.sha256(b"replacement").hexdigest())
    assert destination.read_bytes() == b"existing"


def test_copy_refuses_a_replaced_partial_and_preserves_foreign_work(tmp_path):
    destination = tmp_path / "original.mp4"
    partial = destination.with_suffix(".mp4.part")

    class Replaced(io.BytesIO):
        changed = False

        def read(self, *args):
            if not self.changed:
                self.changed = True
                partial.rename(tmp_path / "retained-original.part")
                partial.write_bytes(b"foreign work must survive")
            return super().read(*args)

    with pytest.raises(ValueError):
        copy_original(Replaced(b"original"), destination, 8, hashlib.sha256(b"original").hexdigest())
    assert partial.read_bytes() == b"foreign work must survive"
    assert not destination.exists()


def test_zip_archive_remains_locked_through_extraction(tmp_path, monkeypatch):
    from pixav.pixel_injector import canary_download as module

    archive = tmp_path / "download.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("canary.mp4", b"original")
    copy = module.copy_original

    def inspect(*args):
        with pytest.raises(BlockingIOError), open_media_file(archive, exclusive=True):
            pytest.fail("archive reader was not locked")
        return copy(*args)

    monkeypatch.setattr(module, "copy_original", inspect)
    extract_original(archive, tmp_path / "original.mp4", "canary.mp4", 8, hashlib.sha256(b"original").hexdigest())


def test_copy_output_is_locked_and_an_interruption_retains_it(tmp_path):
    destination = tmp_path / "original.mp4"
    partial = destination.with_suffix(".mp4.part")

    class Interrupted(BaseException):
        pass

    class Stream(io.BytesIO):
        def read(self, *args):
            with pytest.raises(BlockingIOError), open_media_file(partial):
                pytest.fail("copy output was exposed to another reader")
            raise Interrupted

    with pytest.raises(Interrupted):
        copy_original(Stream(), destination, 8, hashlib.sha256(b"original").hexdigest())
    assert partial.exists() and partial.stat().st_mode & 0o777 == 0o600
    assert not destination.exists()


def test_copy_never_overwrites_preexisting_pending_work(tmp_path):
    destination = tmp_path / "original.mp4"
    partial = destination.with_suffix(".mp4.part")
    partial.write_bytes(b"earlier work")
    with pytest.raises(OriginalWorkConflictError):
        copy_original(io.BytesIO(b"original"), destination, 8, hashlib.sha256(b"original").hexdigest())
    assert partial.read_bytes() == b"earlier work" and not destination.exists()
