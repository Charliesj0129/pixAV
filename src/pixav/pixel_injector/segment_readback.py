"""Cold read-back of one managed segment, in a process that has nothing else.

This module is the entry point of the read-back container. Its whole purpose is
what it cannot reach: the container mounts ``/work`` and a read-only copy of the
source tree, and nothing else — no staging root, no guest ``/data``, no database
credential, no queue. The bytes it reports on can therefore only have come from
the provider, which is what ``cold_inputs: provider-only`` claims.

The manifest arrives on stdin; the receipt leaves on stdout. A failure prints
the exception *type* only, because the message could carry a share location or
a filesystem path.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pixav.pixel_injector.parts_download import download_original
from pixav.shared.storage_models import RemoteAssetSegment

WORK_ROOT = Path("/work")


def read_back(manifest: dict, root: Path) -> dict:
    """Fetch this segment from the provider and return its integrity receipt."""
    segment = RemoteAssetSegment.model_validate(manifest["segment"])
    if not segment.share_url:
        raise ValueError("nothing to read back: the segment has no share location")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("read-back root must be a real directory")
    return {**download_original(segment, root), "cold_inputs": "provider-only"}


def main() -> int:
    os.umask(0o077)
    try:
        print(json.dumps(read_back(json.load(sys.stdin), WORK_ROOT)))
        return 0
    except Exception as exc:  # noqa: BLE001 - the type is the only safe detail
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
