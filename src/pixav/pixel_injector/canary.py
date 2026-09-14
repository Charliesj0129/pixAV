"""Isolated Photos canary preparation and strict evidence evaluation.

This module never connects to the domain database or starts a queue worker.
UI calibration is a required live gate, not an inferred upload success.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

import docker
from pixav.pixel_injector.profiles import get_profile

TOOLS_IMAGE = "pixav-photos-canary:maestro-2.10.0"
OWNER_LABEL = "pixav.photos_canary.owner"


class CanaryBlockedError(ValueError):
    """Safe, fixed diagnostic suitable for public output."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    profile = get_profile("gphotos_pixel_xl_v1", path=profile_path)
    owner = checkpoint["owner"]
    existing = owned_containers(client, owner)
    if existing:
        # Never recreate a possibly credentialed or uploading guest after interruption.
        if checkpoint["status"] != "USER_ACTION_REQUIRED":
            checkpoint["status"] = "REVIEW_REQUIRED"
        save()
        return
    if checkpoint["status"] != "PREFLIGHT_READY":
        raise CanaryBlockedError("runtime creation already attempted; review checkpoint before recovery")
    checkpoint["status"] = "CREATING_RUNTIME"
    save()  # Persist intent before any side effect; labels allow crash recovery.
    guest = client.containers.run(
        profile.image,
        command=list(profile.args),
        name=f"pixav-photos-canary-{owner}",
        detach=True,
        privileged=True,
        labels={OWNER_LABEL: owner, "pixav.photos_canary.role": "guest"},
        ports={"5555/tcp": ("127.0.0.1", None)},
    )
    checkpoint["guest_id"] = guest.id
    save()
    runner = client.containers.run(
        checkpoint["tools_image"],
        name=f"pixav-photos-canary-tools-{owner}",
        detach=True,
        network_mode=f"container:{guest.id}",
        labels={OWNER_LABEL: owner, "pixav.photos_canary.role": "tools"},
        tmpfs={"/tmp": "rw,exec,nosuid,nodev,mode=1777"},  # noqa: S108 - private container tmpfs
    )
    checkpoint["runner_id"] = runner.id
    checkpoint["status"] = "BLOCKED_UI_CALIBRATION"
    save()


def probe_source(client: Any, source: Path, tools_image: str) -> Media:
    output = client.containers.run(
        tools_image,
        command=["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", "/source.mp4"],
        volumes={str(source): {"bind": "/source.mp4", "mode": "ro"}},
        network_disabled=True,
        remove=True,
        user=f"{os.getuid()}:{os.getgid()}",
        labels={"pixav.photos_canary.role": "source-probe"},
    )
    raw = json.loads(output)
    video = next(stream for stream in raw["streams"] if stream["codec_type"] == "video")
    result = Media(
        size=source.stat().st_size,
        sha256=sha256(source),
        width=video["width"],
        height=video["height"],
        codec=video["codec_name"],
        duration=float(raw["format"]["duration"]),
    )
    if result.width < 3840 or result.height < 2160:
        raise CanaryBlockedError("source must be at least 3840x2160")
    return result


def preflight(client: Any, args: argparse.Namespace, checkpoint_path: Path) -> dict[str, Any]:
    if not args.profile.is_file():
        raise CanaryBlockedError("explicit Android profile file is required")
    profile = get_profile("gphotos_pixel_xl_v1", path=args.profile)
    if not profile.image.startswith("sha256:"):
        raise CanaryBlockedError("canary requires an immutable local Android image ID")
    client.images.get(profile.image)
    client.images.get(TOOLS_IMAGE)
    source = args.source.resolve(strict=True)
    if not source.is_file():
        raise CanaryBlockedError("source must be a regular file")
    identity = {"sha256": sha256(source), "size": source.stat().st_size}
    profile_sha = sha256(args.profile)
    if checkpoint_path.exists():
        checkpoint = read_private(checkpoint_path)
        if checkpoint["source"] != identity or checkpoint["profile_sha256"] != profile_sha:
            raise CanaryBlockedError("source or profile changed since checkpoint")
    else:
        if args.command != "preflight":
            raise CanaryBlockedError("run preflight first")
        media = probe_source(client, source, client.images.get(TOOLS_IMAGE).id)
        if media.sha256 != identity["sha256"] or media.size != identity["size"]:
            raise CanaryBlockedError("source changed during preflight")
        checkpoint = {
            "media": media.model_dump(),
            "version": 1,
            "owner": str(uuid.uuid4()),
            "source": identity,
            "profile_sha256": profile_sha,
            "android_image": profile.image,
            "tools_image": client.images.get(TOOLS_IMAGE).id,
            "status": "PREFLIGHT_READY",
            "gates": {key: "OPEN" for key in ("upload", "original", "quota", "automation")},
        }
        write_private(checkpoint_path, checkpoint)
    return checkpoint


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["preflight", "run", "resume", "verify", "cleanup"], nargs="?", default="preflight"
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--secret", type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    try:
        with single_flight():
            root = private_directory(args.evidence)
            checkpoint_path = root / "checkpoint.json"
            client = cast(Any, docker).from_env()
            try:
                if args.command == "cleanup":
                    checkpoint = read_private(checkpoint_path)
                    # Removes only exact owner-labelled runtimes, never files or cloud items.
                    for container in owned_containers(client, checkpoint["owner"]):
                        container.remove(force=True, v=True)
                    checkpoint["status"] = "RUNTIME_REMOVED"
                    write_private(checkpoint_path, checkpoint)
                else:
                    checkpoint = preflight(client, args, checkpoint_path)
                    if args.command in {"run", "resume"}:
                        if args.secret is None:
                            raise CanaryBlockedError("dedicated Google secret path is required")
                        secret = read_private(args.secret)
                        if set(secret) != {"email", "password"} or not all(
                            isinstance(value, str) and value for value in secret.values()
                        ):
                            raise CanaryBlockedError("secret requires nonempty email and password fields")
                        del secret
                        if args.command == "resume" and checkpoint.get("upload_completed_at"):
                            from pixav.pixel_injector.canary_acceptance import resume_existing

                            resume_existing(client, checkpoint, root)
                            write_private(checkpoint_path, checkpoint)
                        else:
                            prepare_runtime(
                                client, checkpoint, args.profile, lambda: write_private(checkpoint_path, checkpoint)
                            )
                    if args.command == "verify":
                        from pixav.pixel_injector.canary_acceptance import verify_existing

                        verify_existing(client, checkpoint, root)
                        write_private(checkpoint_path, checkpoint)
                print(json.dumps({"status": checkpoint["status"], "gates": checkpoint["gates"]}))
                return 0 if args.command in {"preflight", "cleanup"} else 2
            finally:
                client.close()
    except Exception as exc:
        # Docker, UI and browser errors can include account details or signed URLs.
        result = {"status": "BLOCKED", "error_type": type(exc).__name__}
        if isinstance(exc, CanaryBlockedError):
            result["reason"] = str(exc)
        print(json.dumps(result))
        return 2


if __name__ == "__main__":
    # Collectors import this module; use one exception/model identity under -m too.
    from pixav.pixel_injector.canary import main as entrypoint

    raise SystemExit(entrypoint())
