#!/usr/bin/env python3
"""Prepare a new managed qBittorrent config and file secret; dry-run by default."""

from __future__ import annotations

import argparse
import base64
import configparser
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

QBIT_IMAGE = "lscr.io/linuxserver/qbittorrent@sha256:304b19cf94bf4fda534e0b086cab9c5f1a9e139a8180c05c0ad7d2ba1526fa99"
QBIT_SETTINGS = {
    "Application": {"FileLogger\\Enabled": "false"},
    "BitTorrent": {
        "Session\\Interface": "wg0",
        "Session\\InterfaceName": "wg0",
        "Session\\DefaultSavePath": "/downloads",
        "Session\\TempPath": "/downloads/incomplete",
        "Session\\TempPathEnabled": "true",
        "Session\\QueueingSystemEnabled": "true",
        "Session\\MaxActiveDownloads": "1",
        "Session\\DHTEnabled": "true",
        "Session\\PeXEnabled": "true",
        "Session\\LSDEnabled": "false",
    },
    "LegalNotice": {"Accepted": "true"},
    "Preferences": {
        "Connection\\UPnP": "false",
        "WebUI\\Address": "*",
        "WebUI\\Port": "8080",
        "WebUI\\Username": "pixav",
        "WebUI\\LocalHostAuth": "true",
        "WebUI\\AuthSubnetWhitelistEnabled": "false",
        "WebUI\\CSRFProtection": "true",
        "WebUI\\HostHeaderValidation": "true",
        "WebUI\\ReverseProxySupportEnabled": "false",
        "WebUI\\ServerDomains": "gluetun",
    },
}


def private_path(path: Path, uid: int, *, directory: bool) -> None:
    if (
        not path.is_absolute()
        or path.resolve() != path
        or not path.exists()
        or path.stat().st_uid != uid
        or (path.is_dir() if directory else path.is_file()) is not True
        or stat.S_IMODE(path.stat().st_mode) != (0o700 if directory else 0o600)
    ):
        raise ValueError("QBIT_PRIVATE_PATH_REQUIRED")


def validate_qbit_files(data_root: Path, secrets_root: Path, uid: int) -> None:
    config_root = data_root / "qbittorrent"
    for folder in [secrets_root, config_root, config_root / "qBittorrent"]:
        private_path(folder, uid, directory=True)
    conf, credential = config_root / "qBittorrent/qBittorrent.conf", secrets_root / "qbit_password"
    for file in [conf, credential]:
        private_path(file, uid, directory=False)
    if conf.stat().st_size > 131072 or credential.stat().st_size > 4096:
        raise ValueError("QBIT_PRIVATE_CONFIG_INVALID")
    password = credential.read_text().rstrip("\r\n")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", password):
        raise ValueError("QBIT_PRIVATE_CREDENTIAL_INVALID")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str  # type: ignore[method-assign]
    parser.read_string(conf.read_text())
    for section, values in QBIT_SETTINGS.items():
        for key, expected in values.items():
            observed = parser.get(section, key, fallback="").strip('"')
            if key in {"Session\\DefaultSavePath", "Session\\TempPath"}:
                observed = observed.rstrip("/")
            if observed != expected:
                raise ValueError("QBIT_REQUIRED_SETTING_CHANGED")
    _verify_password(parser.get("Preferences", "WebUI\\Password_PBKDF2", fallback="").strip('"'), password)


def _verify_password(value: str, password: str) -> None:
    match = re.fullmatch(r"@ByteArray\(([A-Za-z0-9+/=]+):([A-Za-z0-9+/=]+)\)", value)
    if match is None:
        raise ValueError("QBIT_PASSWORD_BINDING_INVALID")
    salt, stored = (base64.b64decode(part, validate=True) for part in match.groups())
    if len(salt) != 16 or len(stored) != 64:
        raise ValueError("QBIT_PASSWORD_BINDING_INVALID")
    derived = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100_000, dklen=64)
    if not hmac.compare_digest(derived, stored):
        raise ValueError("QBIT_PASSWORD_BINDING_INVALID")


def _write_new(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _require_stopped(context: str, daemon: str, config: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise ValueError("QBIT_DOCKER_CLI_REQUIRED")

    def query(*args: str) -> str:
        return subprocess.run(  # noqa: S603 -- fixed Docker CLI, no credentials or shell
            [docker, "--context", context, *args],
            capture_output=True,
            check=True,
            text=True,
            timeout=15,
        ).stdout.strip()

    if json.loads(query("info", "--format", "{{json .ID}}")) != daemon:
        raise ValueError("QBIT_DAEMON_IDENTITY_CHANGED")
    active = query("ps", "--quiet", "--no-trunc").splitlines()
    if active:
        for row in query("inspect", "--format", "{{json .Mounts}}", *active).splitlines():
            if any(
                mount.get("RW") is not False
                and (config.is_relative_to(Path(mount["Source"])) or Path(mount["Source"]).is_relative_to(config))
                for mount in json.loads(row)
                if mount.get("Source")
            ):
                raise ValueError("QBIT_CONFIG_STILL_MOUNTED_BY_RUNNING_CONTAINER")


def prepare(data_root: Path, secrets_root: Path, *, context: str, daemon: str, apply: bool = False) -> dict:
    uid = os.geteuid()
    for folder in [data_root, secrets_root]:
        private_path(folder, uid, directory=True)
    config = data_root / "qbittorrent"
    _require_stopped(context, daemon, config)
    if config.exists() and (config / "qBittorrent/qBittorrent.conf").exists():
        validate_qbit_files(data_root, secrets_root, uid)
        return {"result": "ALREADY_PREPARED", "files_created": 0, "data_changed": False}
    if (
        (secrets_root / "qbit_password").exists()
        or (secrets_root / "qbit_password").is_symlink()
        or config.is_symlink()
        or (config.exists() and any(config.iterdir()))
    ):
        raise ValueError("QBIT_EXISTING_OR_PARTIAL_CONFIG_REQUIRES_RECONCILIATION")
    if config.exists():
        private_path(config, uid, directory=True)
    if not apply:
        return {"result": "DRY_RUN", "planned_files": 2, "files_created": 0, "data_changed": False}
    password = secrets.token_urlsafe(48)
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100_000, dklen=64)
    verifier = "@ByteArray(" + base64.b64encode(salt).decode() + ":" + base64.b64encode(derived).decode() + ")"
    if not config.exists():
        config.mkdir(mode=0o700)
    (config / "qBittorrent").mkdir(mode=0o700)
    _write_new(secrets_root / "qbit_password", password + "\n")
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[method-assign]
    parser.read_dict(QBIT_SETTINGS)
    parser["Preferences"]["WebUI\\Password_PBKDF2"] = '"' + verifier + '"'
    from io import StringIO

    content = StringIO()
    parser.write(content, space_around_delimiters=False)
    _write_new(config / "qBittorrent/qBittorrent.conf", content.getvalue())
    validate_qbit_files(data_root, secrets_root, uid)
    return {"result": "PREPARED", "files_created": 2, "data_changed": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--daemon-id", required=True)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--secrets-root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = prepare(
            args.data_root, args.secrets_root, context=args.context, daemon=args.daemon_id, apply=args.apply
        )
    except Exception as error:
        code = (
            str(error)
            if type(error) is ValueError and re.fullmatch(r"[A-Z_]+", str(error))
            else "QBIT_PREPARE_UNCONFIRMED"
        )
        print(json.dumps({"result": "REFUSED", "code": code}))
        raise SystemExit(2) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
