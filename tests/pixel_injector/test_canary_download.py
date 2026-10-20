"""Verify original byte validation against real ZIP streams."""

import hashlib
import io
import json
import os
import zipfile
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

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


def test_canary_probe_holds_and_inherits_the_same_reader(tmp_path, monkeypatch):
    from pixav.pixel_injector import canary_download as module

    source = tmp_path / "synthetic.mp4"
    source.write_bytes(b"synthetic media")
    os.utime(source, ns=(1_000_000_000, source.stat().st_mtime_ns))
    before = source.stat()

    def run(command, **kwargs):
        with pytest.raises(BlockingIOError), open_media_file(source, exclusive=True):
            pytest.fail("canary probe source was not locked")
        descriptor = int(command[-1].removeprefix("/proc/self/fd/"))
        assert descriptor in kwargs["pass_fds"]
        # FFprobe reopens this path, so a real read may advance atime even
        # though the inherited descriptor keeps the reader lock alive.
        with open(command[-1], "rb") as reader:
            assert reader.read() == b"synthetic media"
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 3840,
                            "height": 2160,
                            "r_frame_rate": "25/1",
                        }
                    ],
                    "format": {"duration": "12.0"},
                }
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.probe(source)["duration"] == 12
    after = source.stat()
    assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def test_canary_probe_rejects_a_source_replaced_while_the_tool_is_running(tmp_path, monkeypatch):
    from pixav.pixel_injector import canary_download as module

    source = tmp_path / "synthetic.mp4"
    source.write_bytes(b"synthetic media")

    def run(command, **kwargs):
        source.rename(tmp_path / "retained-original.mp4")
        source.write_bytes(b"foreign replacement")
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 3840,
                            "height": 2160,
                            "r_frame_rate": "25/1",
                        }
                    ],
                    "format": {"duration": "12.0"},
                }
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(ValueError):
        module.probe(source)
    assert source.read_bytes() == b"foreign replacement"


@pytest.mark.parametrize("fault", [None, "replacement", "decode-diagnostic"])
def test_canary_collector_keeps_one_source_locked_through_decode_and_receipt(tmp_path, monkeypatch, fault):
    import sys

    from pixav.pixel_injector import canary_download as module

    data = b"synthetic provider original"
    expected = {
        "filename": "synthetic.mp4",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "width": 3840,
        "height": 2160,
        "codec": "h264",
        "duration": 12.0,
    }
    root = tmp_path / "cold-work"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **kwargs: str(root))
    monkeypatch.setenv("CANARY_EXPECTED", json.dumps(expected))
    monkeypatch.setenv("PHOTOS_SHARE_URL", "https://photos.app.goo.gl/synthetic")

    def save(path):
        with zipfile.ZipFile(path, "w") as bundle:
            bundle.writestr(expected["filename"], data)

    browser = MagicMock(version="synthetic-browser")
    page = browser.new_context.return_value.new_page.return_value
    download = MagicMock()
    download.save_as.side_effect = save
    page.expect_download.return_value.__enter__.return_value.value = download
    manager = MagicMock()
    manager.__enter__.return_value.chromium.launch.return_value = browser
    playwright = ModuleType("playwright.sync_api")
    playwright.sync_playwright = lambda: manager
    monkeypatch.setitem(sys.modules, "playwright.sync_api", playwright)
    calls = []
    descriptors = []
    original = root / "cloud-original.mp4"

    def check_locked():
        with pytest.raises(BlockingIOError), open_media_file(original, exclusive=True):
            pytest.fail("collector released the original before its receipt was committed")

    def run(command, **kwargs):
        assert browser.close.called
        check_locked()
        argument = command[-1] if "ffprobe" in command[0] else command[command.index("-i") + 1]
        descriptor = int(argument.removeprefix("/proc/self/fd/"))
        assert descriptor in kwargs["pass_fds"]
        descriptors.append(descriptor)
        if "ffprobe" in command[0]:
            calls.append("probe")
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "h264",
                                "width": 3840,
                                "height": 2160,
                                "r_frame_rate": "25/1",
                            }
                        ],
                        "format": {"duration": "12.0"},
                    }
                ).encode(),
                stderr=b"",
            )
        calls.append("decode")
        assert kwargs["timeout"] == 180
        if fault == "replacement":
            original.rename(root / "retained-original.mp4")
            original.write_bytes(b"foreign bytes")
        elif fault == "decode-diagnostic":
            kwargs["stderr"].write(b"synthetic decode diagnostic")
        return SimpleNamespace(returncode=0)

    dump = module.json.dump

    def commit(*args, **kwargs):
        check_locked()
        calls.append("receipt")
        return dump(*args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.json, "dump", commit)
    result = module.main()
    assert result == (2 if fault else 0)
    assert calls == (["probe", "decode"] if fault else ["probe", "decode", "receipt"])
    assert len(set(descriptors)) == 1
    assert (root / "report.json").exists() is (fault is None)
    if fault == "replacement":
        assert original.read_bytes() == b"foreign bytes"
