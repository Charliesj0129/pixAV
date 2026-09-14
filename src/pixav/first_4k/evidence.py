"""Append-only evidence writing for the supervisor.

Every file the supervisor produces is opened ``O_EXCL`` at mode 0600: evidence
is written once and never overwritten, and a rerun that would collide fails
instead of quietly replacing what the previous attempt recorded.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE_SUFFIXES = {".py", ".yml", ".yaml"}
SOURCE_DIRECTORIES = ("src", "scripts", "config")


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def write(out: Path, name: str, value: Any) -> None:
    fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(value if isinstance(value, bytes) else json.dumps(value, indent=2, default=str).encode())


def event(out: Path, kind: str, **fields: Any) -> dict:
    item = {"at": stamp(), "event": kind, **fields}
    print(json.dumps(item), flush=True)  # noqa: T201 - the supervisor's log is its stdout
    fd = os.open(out / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as handle:
        handle.write(json.dumps(item) + "\n")
    return item


def command(args: list[str], *, data: bytes | None = None) -> bytes:
    """Run a fixed guarded subprocess, revealing only which program failed."""
    process = subprocess.run(args, input=data, capture_output=True, timeout=120, check=False)  # noqa: S603
    if process.returncode:
        raise RuntimeError("guarded subprocess failed: " + args[0])
    return process.stdout


def source_digests(root: Path) -> dict[str, str]:
    """Hash every source and configuration file the next invocation will load."""
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in SOURCE_DIRECTORIES
        for path in (root / directory).rglob("*")
        if path.is_file() and path.suffix in SOURCE_SUFFIXES
    }


def verify_sources(digests: dict[str, str]) -> None:
    """Refuse to re-enter the CLI when its code changed since the snapshot.

    The supervisor waits for hours between invocations and then re-executes the
    CLI from disk. An edit landing in that window would run a half-finished
    refactor against a live run document, so a changed digest stops the
    supervisor instead: the run stays exactly where it is, for a human to look at.
    """
    for path, digest in digests.items():
        current = Path(path)
        if not current.is_file() or hashlib.sha256(current.read_bytes()).hexdigest() != digest:
            raise RuntimeError("source/config changed while waiting; inspect before continuing")
