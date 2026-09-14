"""Backups must never be world-readable, not even mid-write.

A ``pg_dump`` of this database contains ``accounts.password``. The regression
this guards against is the tempting shape `path.open("x")` followed by a
`chmod`: it produces the right final mode while leaving the credentials
readable by every other user on the host for the whole duration of the dump.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from scripts.backup_files import BACKUP_FILE_MODE, create_backup_file


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestCreateBackupFile:
    def test_file_is_owner_only_before_anything_is_written(self, tmp_path: Path) -> None:
        target = tmp_path / "pixav.dump"

        with create_backup_file(target, binary=True) as handle:
            # Inspect the mode while the file is still empty: this is the window
            # a chmod-afterwards implementation leaves open.
            assert _mode(target) == BACKUP_FILE_MODE
            handle.write(b"payload")

        assert _mode(target) == BACKUP_FILE_MODE
        assert target.read_bytes() == b"payload"

    def test_group_and_other_have_no_access(self, tmp_path: Path) -> None:
        target = tmp_path / "pixav.dump"

        with create_backup_file(target) as handle:
            handle.write("x")

        mode = _mode(target)
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO)

    def test_text_handle_round_trips_utf8(self, tmp_path: Path) -> None:
        target = tmp_path / "selected-rows.json"

        with create_backup_file(target) as handle:
            json.dump({"title": "無碼流出"}, handle, ensure_ascii=False)

        assert json.loads(target.read_text(encoding="utf-8"))["title"] == "無碼流出"

    def test_refuses_to_overwrite_an_existing_backup(self, tmp_path: Path) -> None:
        target = tmp_path / "pixav.dump"
        target.write_bytes(b"first backup")

        with pytest.raises(FileExistsError):
            create_backup_file(target, binary=True)

        assert target.read_bytes() == b"first backup"
