from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
_IMMUTABLE_IMAGE = re.compile(r"^[^@]+:[^@]+@sha256:[0-9a-f]{64}$")


def test_phase0_gate_images_are_tagged_and_digest_pinned() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    for service in ("gluetun", "qbittorrent", "prometheus", "alertmanager"):
        image = compose["services"][service]["image"]
        assert _IMMUTABLE_IMAGE.fullmatch(image), f"{service} image is mutable: {image}"
        assert ":latest@" not in image


def test_production_overlay_does_not_restore_mutable_qbit_default() -> None:
    production = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    qbit_line = next(line.strip() for line in production.splitlines() if "PIXAV_IMAGE_QBITTORRENT" in line)
    assert ":latest" not in qbit_line
    assert "@sha256:" in qbit_line
