"""Opt-in qBittorrent contract in a disposable network without external egress."""

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration
IMAGE = "lscr.io/linuxserver/qbittorrent@sha256:304b19cf94bf4fda534e0b086cab9c5f1a9e139a8180c05c0ad7d2ba1526fa99"


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, check=True).stdout  # noqa: S603,S607


def bencode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    return b"d" + b"".join(bencode(key) + bencode(item) for key, item in sorted(value.items())) + b"e"


async def test_owned_completed_torrent_is_reused_bdd_024_034(integration_db, tmp_path):
    """No provider torrent is used; the sole piece is a synthetic local artifact."""
    if os.getenv("PIXAV_RUN_QBIT_CONTRACT") != "1":
        pytest.skip("set PIXAV_RUN_QBIT_CONTRACT=1 for disposable Docker contract")
    context = docker("context", "show").decode().strip()
    daemon = docker("info", "--format", "{{.ID}}").decode().strip()
    if context != os.getenv("PIXAV_TEST_DOCKER_CONTEXT") or daemon != os.getenv("PIXAV_TEST_DOCKER_ID"):
        raise RuntimeError("Docker identity must match before creating the test resources")
    network = docker("network", "create", "--internal", f"pixav-test-{uuid4().hex}").decode().strip()
    container = None
    try:
        media = tmp_path / "synthetic.mp4"
        media.write_bytes(b"synthetic torrent contract artifact")
        info = {
            b"length": media.stat().st_size,
            b"name": media.name.encode(),
            b"piece length": 16384,
            b"pieces": hashlib.sha1(media.read_bytes()).digest(),  # noqa: S324 -- BitTorrent identity
            b"private": 1,
        }
        torrent_hash = hashlib.sha1(bencode(info)).hexdigest()  # noqa: S324
        container = (
            docker(
                "run",
                "-d",
                "--network",
                network,
                "--memory",
                "512m",
                "--cpus",
                "1",
                "--tmpfs",
                "/config",
                "-e",
                f"PUID={os.getuid()}",
                "-e",
                f"PGID={os.getgid()}",
                "-p",
                "127.0.0.1::8080",
                "-v",
                f"{tmp_path}:/downloads",
                IMAGE,
            )
            .decode()
            .strip()
        )
        networks = json.loads(docker("inspect", container))[0]["NetworkSettings"]["Networks"]
        address = next(iter(networks.values()))["IPAddress"]
        password = None
        for _ in range(45):
            # Startup credentials are kept in memory, never printed or persisted.
            match = re.search(
                r"temporary password is provided for this session: (\S+)", docker("logs", container).decode()
            )
            if match:
                password = match.group(1)
                break
            await asyncio.sleep(1)
        assert password is not None, "isolated qBittorrent did not initialize"
        payload = dict(
            url=f"http://{address}:8080",
            password=password,
            media=str(media),
            torrent=base64.b64encode(bencode({b"info": info})).decode(),
            hash=torrent_hash,
        )
        helper = subprocess.run(  # noqa: S603,S607 -- owned network; credentials only on stdin
            [
                shutil.which("docker") or "/usr/bin/docker",
                "run",
                "--rm",
                "-i",
                "--network",
                network,
                "--memory",
                "512m",
                "--cpus",
                "1",
                "-v",
                f"{Path.cwd()}:/workspace:ro",
                "-v",
                f"{tmp_path}:{tmp_path}:ro",
                "-e",
                "PYTHONPATH=/workspace/src",
                "pixav-media_loader:latest",
                "/app/.venv/bin/python",
                "/workspace/tests/integration/workflow_torrent_contract.py",
            ],
            input=json.dumps(payload).encode(),
            capture_output=True,
        )
        assert helper.returncode == 0, helper.stderr.decode()
        assert b"ownership refusal passed" in helper.stdout
    finally:
        if container:
            docker("rm", "-f", container)
        docker("network", "rm", network)
