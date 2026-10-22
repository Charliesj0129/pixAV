"""Shared canary evidence helpers and a retired Docker canary entry.

Existing collectors keep their models, private evidence and ownership checks.
Legacy provisioning, source probing and CLI operations fail before any I/O.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pixav.pixel_injector.errors import CanaryBlockedError
from pixav.shared.media_file import open_media_file

TOOLS_IMAGE = "pixav-photos-canary:maestro-2.10.0"
OWNER_LABEL = "pixav.photos_canary.owner"


def sha256(path: Path) -> str:
    with open_media_file(path) as source:
        return source.facts()[0]


def private_directory(path: Path) -> Path:
    absolute = path.absolute()
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise CanaryBlockedError("symlink evidence path rejected")
    absolute.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = absolute.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise CanaryBlockedError("evidence directory must be owned by this user with mode 0700")
    return absolute


def write_private(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink():
        raise CanaryBlockedError("symlink checkpoint rejected")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".checkpoint-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_private(path: Path) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise CanaryBlockedError("private input must be an owned regular 0600 file")
        result = json.load(stream)
    if not isinstance(result, dict):
        raise CanaryBlockedError("private input must be a JSON object")
    return result


@contextmanager
def single_flight() -> Iterator[None]:
    # Global per operator, including runs using different evidence directories.
    path = Path(tempfile.gettempdir()) / f"pixav-photos-canary-{os.getuid()}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise CanaryBlockedError("unsafe single-flight lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class Media(BaseModel):
    model_config = ConfigDict(extra="forbid")
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    codec: str
    duration: float = Field(gt=0, allow_inf_nan=False)


class Observation(BaseModel):
    """Explicit measured inputs; rounded account totals alone never prove exemption."""

    model_config = ConfigDict(extra="forbid")
    source_sha256: str
    filename: str
    backed_up: bool
    original_quality: bool
    uploaded_at: float = Field(ge=0, allow_inf_nan=False)
    before_at: float = Field(ge=0, allow_inf_nan=False)
    after_at: float = Field(ge=0, allow_inf_nan=False)
    before_bytes: int = Field(ge=0)
    after_bytes: int = Field(ge=0)
    precision_bytes: int = Field(gt=0)
    item_charged_bytes: int | None = Field(default=None, ge=0)
    independent_browser_download: bool = False
    decode_passed: bool = False


def evaluate(source: Media, cloud: Media, observation: Observation, filename: str) -> dict[str, str]:
    """Evaluate measurements, not their provenance; collectors must retain live evidence."""
    identity = observation.source_sha256 == source.sha256 and observation.filename == filename
    upload = identity and observation.backed_up and observation.original_quality
    original = source == cloud and source.width >= 3840 and source.height >= 2160
    original = original and observation.independent_browser_download and observation.decode_passed
    quota = "OPEN"
    if observation.after_bytes > observation.before_bytes or (observation.item_charged_bytes or 0) > 0:
        quota = "FAIL"
    elif (
        upload
        and observation.before_at < observation.uploaded_at
        and observation.after_at >= observation.uploaded_at + 86400
        and observation.after_bytes == observation.before_bytes
        and observation.precision_bytes < source.size
        and observation.item_charged_bytes == 0
    ):
        quota = "PASS"
    return {
        "upload": "PASS" if upload else "FAIL",
        "original": "PASS" if upload and original else "FAIL",
        "quota": quota,
    }


def owned_containers(client: Any, owner: str) -> list[Any]:
    if not owner or str(uuid.UUID(owner)) != owner:
        raise CanaryBlockedError("invalid runtime owner")
    containers = client.containers.list(all=True, filters={"label": f"{OWNER_LABEL}={owner}"})
    for container in containers:
        if container.labels.get(OWNER_LABEL) != owner or "pixav.task_id" in container.labels:
            raise CanaryBlockedError("runtime ownership mismatch")
    return list(containers)


def prepare_runtime(client: Any, checkpoint: dict[str, Any], profile_path: Path, save: Any) -> None:
    """Reject historical callers without inspecting or recreating their guest."""
    raise CanaryBlockedError("LEGACY_DOCKER_CANARY_RETIRED")


def probe_source(client: Any, source: Path, tools_image: str) -> Media:
    """The retired probe must never create a container or reopen its source."""
    raise CanaryBlockedError("LEGACY_DOCKER_CANARY_RETIRED")


def preflight(client: Any, args: argparse.Namespace, checkpoint_path: Path) -> dict[str, Any]:
    """Keep a refusal for historical callers; no checkpoint is read or written."""
    raise CanaryBlockedError("LEGACY_DOCKER_CANARY_RETIRED")


def main() -> int:
    """Retire every legacy verb before parsing paths, secrets or Docker state."""
    print(json.dumps({"status": "RETIRED", "reason_code": "LEGACY_DOCKER_CANARY_RETIRED"}))
    return 2


if __name__ == "__main__":
    # Collectors import this module; use one exception/model identity under -m too.
    from pixav.pixel_injector.canary import main as entrypoint

    raise SystemExit(entrypoint())
