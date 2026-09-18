from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_maxwell_disk_guard_sees_shared_download_filesystem() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    maxwell_volumes = compose["services"]["maxwell_core"].get("volumes", [])
    media_loader_volumes = compose["services"]["media_loader"].get("volumes", [])
    qbit_volumes = compose["services"]["qbittorrent"].get("volumes", [])

    assert "./data:/app/data" in maxwell_volumes
    assert "./data:/app/data" in media_loader_volumes
    assert "./data/downloads:/downloads" in qbit_volumes
