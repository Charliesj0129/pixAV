"""Verify a cleanup projection in a private, read-only Linux mount namespace.

This helper only reads the projection. Missing util-linux tools, unavailable
user namespaces, nested mounts or an unconfirmed child all refuse cleanup.
It never changes the caller's mounts and does not inherit account credentials.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from pixav.shared.media_file import open_media_directory


def _identity(descriptor: int) -> tuple[int, int]:
    info = os.fstat(descriptor)
    return info.st_dev, info.st_ino


def verify_in_readonly_namespace(root: Path, video_id: UUID, version: str, digest: str) -> bool:
    unshare, mount = shutil.which("unshare"), shutil.which("mount")
    if unshare is None or mount is None:
        return False
    # Keep Python's code/dependency paths, with no operator secrets or service
    # settings inherited through the environment. Do not create bytecode files.
    environment = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(os.path.abspath(entry or os.getcwd()) for entry in sys.path),
    }
    try:
        with open_media_directory(root / ".pixav-cleanup-root") as directory:
            result = subprocess.run(  # noqa: S603 -- fixed util-linux/Python commands, no shell
                [
                    unshare,
                    "--user",
                    "--map-root-user",
                    "--mount",
                    "--propagation=private",
                    "--fork",
                    "--kill-child=KILL",
                    "--",
                    sys.executable,
                    "-m",
                    "pixav.library_projection.readonly_cleanup",
                    str(directory.descriptor),
                    str(root),
                    str(video_id),
                    version,
                    digest,
                    mount,
                ],
                pass_fds=(directory.descriptor,),
                env=environment,
                cwd="/",
                capture_output=True,
                timeout=15,
                check=False,
            )
            directory.assert_current()
            return result.returncode == 0 and result.stdout == b"VERIFIED\n" and not result.stderr
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _has_nested_mount(root: Path) -> bool:
    # A nonrecursive bind could hide an existing nested mount. Refuse that
    # layout before binding; do not verify a different underlying directory.
    with Path("/proc/self/mountinfo").open() as rows:
        for row in rows:
            mountpoint = re.sub(r"\\([0-7]{3})", lambda value: chr(int(value[1], 8)), row.split()[4])
            location = Path(mountpoint)
            if location != root and root in location.parents:
                return True
    return False


def _verify_child(arguments: list[str]) -> bool:
    from pixav.library_projection.writer import _publication_on_readonly_mount

    if len(arguments) != 6:
        return False
    descriptor, path, video, version, digest, mount = arguments
    opened, root = int(descriptor), Path(path)
    if not root.is_absolute() or os.path.abspath(root) != str(root) or not Path(mount).is_absolute():
        return False
    if not stat.S_ISDIR(os.fstat(opened).st_mode):
        return False
    with open_media_directory(root / ".pixav-cleanup-root") as directory:
        if _identity(opened) != _identity(directory.descriptor) or _has_nested_mount(root):
            return False
        commands = (
            [mount, "--no-canonicalize", "--bind", str(root), str(root)],
            [mount, "--no-canonicalize", "--options", "remount,bind,ro", str(root)],
        )
        for command in commands:
            result = subprocess.run(  # noqa: S603 -- fixed local mount operations in this private namespace
                command, pass_fds=(opened,), capture_output=True, timeout=5, check=False
            )
            if result.returncode or result.stdout or result.stderr:
                return False
        directory.assert_current()
        if not os.statvfs(root).f_flag & os.ST_RDONLY:
            return False
        return _publication_on_readonly_mount(root, UUID(video), version, digest)


def main() -> None:
    try:
        verified = _verify_child(sys.argv[1:])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        verified = False
    print("VERIFIED" if verified else "REFUSED")
    raise SystemExit(0 if verified else 1)


if __name__ == "__main__":
    main()
