"""Verify original byte validation against real ZIP streams."""

import hashlib
import io
import zipfile

import pytest

from pixav.pixel_injector.canary_download import copy_original, extract_original


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
