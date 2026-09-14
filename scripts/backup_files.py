"""Owner-only creation of backup files.

Every backup this repository writes can contain material that must not be
world-readable: a ``pg_dump`` of this database carries ``accounts.password``,
and a selected-row backup carries the exact rows a destructive script is about
to delete. Creating such a file with the default 0644 umask and chmod-ing it
afterwards leaves it readable by every other user on the host for the whole
duration of the write, which for a full dump is the interesting window.

``O_EXCL`` additionally preserves the never-overwrite guarantee both callers
rely on: a second run with the same timestamp fails instead of destroying the
first backup.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

BACKUP_FILE_MODE = 0o600


def create_backup_file(path: Path, *, binary: bool = False) -> IO:
    """Create ``path`` owner-only, failing if it already exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, BACKUP_FILE_MODE)
    if binary:
        return os.fdopen(fd, "wb")
    return os.fdopen(fd, "w", encoding="utf-8")
