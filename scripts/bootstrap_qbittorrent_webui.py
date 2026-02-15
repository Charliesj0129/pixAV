#!/usr/bin/env python3
"""Bootstrap qBittorrent WebUI credentials for the linuxserver/qbittorrent image.

Why:
  On first run, linuxserver/qbittorrent uses a temporary WebUI password unless a
  password is configured. That breaks automated workers that expect stable
  credentials (PIXAV_QBIT_USER/PIXAV_QBIT_PASSWORD).

What it does:
  1) Stops the `pixav-qbittorrent` container (if running)
  2) Writes WebUI Username + PBKDF2 password hash into /config/qBittorrent/qBittorrent.conf
     via the container's /config volume
  3) Starts the container again

Defaults:
  - username: admin
  - password: PIXAV_QBIT_PASSWORD or "adminadmin"
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys

CONTAINER_NAME = os.getenv("PIXAV_QBIT_CONTAINER", "pixav-qbittorrent")


def _run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True).strip()


def _is_running(container: str) -> bool:
    try:
        return _run(["docker", "inspect", "-f", "{{.State.Running}}", container]) == "true"
    except subprocess.CalledProcessError:
        raise RuntimeError(f"container not found: {container}")


def _get_config_volume(container: str) -> str:
    raw = _run(["docker", "inspect", container])
    data = json.loads(raw)
    if not data:
        raise RuntimeError(f"container inspect returned empty: {container}")

    mounts = data[0].get("Mounts", [])
    for mount in mounts:
        if mount.get("Destination") == "/config" and mount.get("Type") == "volume":
            name = mount.get("Name")
            if isinstance(name, str) and name:
                return name
    raise RuntimeError("cannot locate /config named volume; expected a docker volume mount")


def _pbkdf2_qbittorrent(password: str) -> str:
    """Return the @ByteArray(salt:hash) payload used by qBittorrent WebUI PBKDF2."""
    # qBittorrent uses PBKDF2-HMAC-SHA512 with a random 16-byte salt.
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, 100_000, dklen=64)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    derived_b64 = base64.b64encode(derived).decode("ascii")
    return f"@ByteArray({salt_b64}:{derived_b64})"


def main() -> int:
    username = os.getenv("PIXAV_QBIT_USER", "admin").strip() or "admin"
    password = os.getenv("PIXAV_QBIT_PASSWORD", "adminadmin")
    pbkdf2 = _pbkdf2_qbittorrent(password)

    volume = _get_config_volume(CONTAINER_NAME)

    if _is_running(CONTAINER_NAME):
        subprocess.check_call(["docker", "stop", CONTAINER_NAME])

    # Edit config while qBittorrent is stopped; otherwise it overwrites on shutdown.
    subprocess.check_call(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            f"QBIT_USER={username}",
            "-e",
            f"QBIT_PBKDF2={pbkdf2}",
            "-v",
            f"{volume}:/config",
            "python:3.12-slim",
            "python",
            "-c",
            (
                "from pathlib import Path\n"
                "import os\n"
                "conf = Path('/config/qBittorrent/qBittorrent.conf')\n"
                "text = conf.read_text(encoding='utf-8') if conf.exists() else ''\n"
                "lines = text.splitlines()\n"
                "out = []\n"
                "for line in lines:\n"
                "    if line.startswith('WebUI\\\\\\\\Password_'):\n"
                "        continue\n"
                "    if line.startswith('WebUI\\\\\\\\Username='):\n"
                "        continue\n"
                "    out.append(line)\n"
                "if '[Preferences]' not in text:\n"
                "    out.append('')\n"
                "    out.append('[Preferences]')\n"
                "out.append(f\"WebUI\\\\\\\\Username={os.environ['QBIT_USER']}\")\n"
                'out.append(f"WebUI\\\\\\\\Password_PBKDF2=\\"{os.environ[\'QBIT_PBKDF2\']}\\"")\n'
                "conf.parent.mkdir(parents=True, exist_ok=True)\n"
                "conf.write_text('\\n'.join(out).rstrip() + '\\n', encoding='utf-8')\n"
            ),
        ]
    )

    subprocess.check_call(["docker", "start", CONTAINER_NAME])
    print("qBittorrent WebUI credentials bootstrapped.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
