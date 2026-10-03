"""Deployment routing uses one context/project and never starts host executors."""

import json

# Fixed repository scripts and test-controlled arguments only.
# ruff: noqa: S603
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("mode", ["vpn", "direct"])
def test_entry_routes_all_commands_to_explicit_instance(tmp_path, mode):
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    docker.chmod(0o700)
    result = subprocess.run(
        [
            str(ROOT / "scripts/start_workers.sh"),
            "--mode",
            mode,
            "--context",
            "test-context",
            "--project",
            "test-project",
            "config",
            "-q",
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "COMPOSE_PROFILES": ""},
        capture_output=True,
        text=True,
        check=True,
    )
    args = result.stdout.splitlines()
    assert args[:5] == ["--context", "test-context", "compose", "--project-name", "test-project"]
    assert (str(ROOT / "docker-compose.direct.yml") in args) == (mode == "direct")
    assert args[-2:] == ["config", "-q"]


@pytest.mark.parametrize("args", [[], ["--mode", "bad"], ["--mode"], ["--mode", "direct", "up"]])
def test_entry_refuses_ambiguous_deployment(args):
    assert subprocess.run([str(ROOT / "scripts/start_workers.sh"), *args], capture_output=True).returncode == 2


@pytest.mark.parametrize("mode", ["vpn", "direct"])
def test_resolved_production_network_contract(mode):
    cmd = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.prod.yml"]
    if mode == "direct":
        cmd += ["-f", "docker-compose.direct.yml"]
    result = subprocess.run(cmd + ["config", "--format", "json"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, "Compose config failed; inspect locally without exposing resolved secrets"
    services = json.loads(result.stdout)["services"]
    storage = services["storage_worker"]
    assert float(storage["cpus"]) > 0
    assert int(storage["mem_limit"]) == int(storage["memswap_limit"]) > 0
    for role in ("GUEST", "TOOLS"):
        assert float(storage["environment"][f"PIXAV_STORAGE_{role}_CPUS"]) > 0
        assert int(storage["environment"][f"PIXAV_STORAGE_{role}_MEMORY_MB"]) >= 512
    qbit = services["qbittorrent"]
    loader = services["media_loader"]
    assert loader["depends_on"]["qbittorrent"]["condition"] == "service_healthy"
    assert loader["environment"]["PIXAV_DB_HOST"] == "postgres"
    if mode == "vpn":
        assert qbit["network_mode"] == "service:gluetun"
        assert qbit["depends_on"]["gluetun"]["condition"] == "service_healthy"
        assert loader["environment"]["PIXAV_QBIT_URL"].startswith("http://gluetun:")
    else:
        assert "network_mode" not in qbit
        assert "gluetun" not in qbit.get("depends_on", {})
        assert "pixav" in qbit["networks"]
        assert loader["environment"]["PIXAV_VPN_EGRESS_ECHO_URL"] == ""
        assert loader["environment"]["PIXAV_QBIT_URL"].startswith("http://qbittorrent:")
        assert "gluetun" not in services and "vpn_sentinel" not in services


def test_direct_rejects_explicit_vpn_service():
    result = subprocess.run(
        [
            str(ROOT / "scripts/start_workers.sh"),
            "--mode",
            "direct",
            "--context",
            "test",
            "--project",
            "test",
            "up",
            "gluetun",
        ],
        capture_output=True,
    )
    assert result.returncode == 2
