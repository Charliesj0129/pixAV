"""Backwards-compatible import path; the helper lives in :mod:`pixav.shared.backup_files`.

Nine operator scripts and tests import this module directly, several of them
through a fallback that also works when ``scripts/`` is run as a plain directory.
Keeping the path valid lets the helper move into the package untouched.
"""

from pixav.shared.backup_files import BACKUP_FILE_MODE, create_backup_file

__all__ = ["BACKUP_FILE_MODE", "create_backup_file"]
