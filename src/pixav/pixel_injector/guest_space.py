"""Guest free-space admission for a segment push.

A push writes the stage copy and then publishes a second copy into the camera
folder, so the guest briefly holds the segment twice. A full ``/data`` leaves a
partial stage file that later needs reconciliation, which is far costlier than
refusing the push before any byte is written.
"""

from __future__ import annotations

import re

GUEST_STAGE_DIRECTORY = "/data/local/tmp"
# Headroom for Photos' own cache and database growth while it backs the copy up.
GUEST_PUSH_MARGIN_BYTES = 512 * 1024 * 1024
_DF_ROW = re.compile(r"^\S+\s+(\d+)\s+(\d+)\s+(\d+)\s+\d+%\s+(\S+)\s*$")


def guest_push_required_bytes(size_bytes: int) -> int:
    """Stage plus published copy, plus the fixed margin."""
    if size_bytes <= 0:
        raise ValueError("segment size must be positive")
    return 2 * size_bytes + GUEST_PUSH_MARGIN_BYTES


def parse_available_bytes(df_output: str) -> int | None:
    """Available bytes from ``df -k <dir>`` output, or None when it cannot be read.

    Exactly one filesystem row is accepted; anything else is unknown space,
    which the caller must treat as insufficient.
    """
    rows = [match for line in df_output.splitlines() if (match := _DF_ROW.match(line.strip()))]
    if len(rows) != 1:
        return None
    return int(rows[0].group(3)) * 1024
