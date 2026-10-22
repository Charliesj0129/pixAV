"""Linux transfer ownership and diagnostics without a device or account."""

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from pixav.pixel_injector import adb_media
from pixav.shared.media_file import file_version, open_media_file


@pytest.fixture
def transfer(tmp_path):
    source = tmp_path / "pixav-synthetic.mp4"
    source.write_bytes(b"original")
    return source, {
        "destination": "/data/local/tmp/" + source.name,
        "serial": "emulator-5554",
        "digest": hashlib.sha256(b"original").hexdigest(),
        "size": 8,
        "version": file_version(source.stat()),
        "timeout": 1,
    }


@pytest.mark.parametrize("fault", ["identity", "digest", "size", "symlink", "parent-symlink", "writer"])
def test_unverified_or_busy_source_never_starts_adb(transfer, tmp_path, monkeypatch, fault):
    source, arguments = transfer
    execute = Mock(side_effect=AssertionError("UNVERIFIED_TRANSFER_STARTED"))
    monkeypatch.setattr(adb_media.subprocess, "run", execute)
    monkeypatch.setattr(adb_media.shutil, "which", lambda name: "/usr/bin/adb")
    if fault == "identity":
        arguments["version"] = tuple(value + 1 for value in arguments["version"])
    elif fault == "digest":
        arguments["digest"] = "0" * 64
    elif fault == "size":
        arguments["size"] = 9
    elif fault == "symlink":
        retained = source.with_suffix(".retained")
        source.rename(retained)
        source.symlink_to(retained)
    elif fault == "parent-symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        source = alias / source.name
    if fault == "writer":
        with open_media_file(source, exclusive=True), pytest.raises(BlockingIOError):
            adb_media.push_locked(source, **arguments)
    else:
        with pytest.raises((OSError, ValueError)):
            adb_media.push_locked(source, **arguments)
    execute.assert_not_called()


def test_noncooperating_content_change_cannot_report_a_verified_copy(transfer, monkeypatch):
    source, arguments = transfer
    monkeypatch.setattr(adb_media.shutil, "which", lambda name: "/usr/bin/adb")

    def execute(command, **kwargs):
        with pytest.raises(BlockingIOError), open_media_file(source, exclusive=True):
            pytest.fail("transfer released its read lock")
        descriptor = int(command[-2].removeprefix("/proc/self/fd/"))
        assert kwargs["pass_fds"] == (descriptor,)
        assert os.pread(descriptor, 8, 0) == b"original"
        source.write_bytes(b"modified")

    monkeypatch.setattr(adb_media.subprocess, "run", execute)
    with pytest.raises(ValueError, match="SOURCE_CHANGED"):
        adb_media.push_locked(source, **arguments)
    assert source.read_bytes() == b"modified"


def test_deadline_reaps_the_reader_and_releases_only_its_lock(transfer, tmp_path, monkeypatch):
    source, arguments = transfer
    binary = tmp_path / "adb"
    marker = tmp_path / "reader-pid"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os,time\nfrom pathlib import Path\n"
        "Path(os.environ['PIXAV_TEST_READER_PID']).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PIXAV_TEST_READER_PID", str(marker))
    with pytest.raises(adb_media.subprocess.TimeoutExpired):
        adb_media.push_locked(source, **arguments)
    assert not Path("/proc", marker.read_text()).exists()
    assert source.read_bytes() == b"original"
    with open_media_file(source, exclusive=True):
        pass


def test_invalid_cli_diagnostics_never_echo_private_arguments(monkeypatch, capsys):
    private_marker = "private-filename-marker"
    monkeypatch.setattr(sys, "argv", ["adb_media", "--size", private_marker])
    assert adb_media.main() == 2
    output = capsys.readouterr()
    assert output.err == "" and private_marker not in output.out
    assert json.loads(output.out) == {
        "status": "REVIEW_REQUIRED",
        "reason_code": "ADB_MEDIA_COPY_UNCONFIRMED",
    }


async def test_generic_adb_entry_cannot_bypass_the_media_binding():
    from pixav.pixel_injector.canary import CanaryBlockedError
    from pixav.pixel_injector.maestro_parts import MaestroPartUploader

    uploader = object.__new__(MaestroPartUploader)
    uploader.windows_device = None
    uploader.check = None
    uploader.runner = Mock()
    uploader.target = Mock(serial="emulator-5554")
    with pytest.raises(CanaryBlockedError, match="verified source binding"):
        await uploader.adb("push", "/parts/unverified.mp4", "/data/local/tmp/unverified.mp4")
    uploader.runner.exec_run.assert_not_called()
