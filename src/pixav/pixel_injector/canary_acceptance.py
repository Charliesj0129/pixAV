"""Acceptance of the calibrated, already uploaded Photos canary.

These collectors do not log in, publish media, or create sharing links.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pixav.pixel_injector.canary import OWNER_LABEL, CanaryBlockedError, read_private, sha256, write_private
from pixav.pixel_injector.canary_maestro import hierarchy


def collect_item(attributes: list[dict[str, str]], filename: str, media: dict[str, Any]) -> dict[str, Any]:
    texts = {a.get("text", "") for a in attributes}
    required = {filename, "Backed up", "Original quality", f"{media['width']} x {media['height']}"}
    if not required <= texts:
        raise CanaryBlockedError("exact backed-up original item information is not visible; UI review required")
    free = any(t.startswith("This item doesn't take up space in your account storage.") for t in texts)
    return {
        "filename": filename,
        "source_sha256": media["sha256"],
        "backed_up": True,
        "original_quality": True,
        "width": media["width"],
        "height": media["height"],
        "zero_item_charge": free,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "collector": "Maestro hierarchy; exact filename and item details",
    }


def quota_verdict(state: dict[str, Any], item: dict[str, Any]) -> str:
    """Compare displayed MB, without pretending Google supplied exact byte totals."""
    before = state.get("quota_before", {})
    after = state.get("quota_after_24h", {})
    pattern = r"([0-9]+\.[0-9]{2}) MB of ([0-9]+) GB"
    first = re.fullmatch(pattern, before.get("display", ""))
    last = re.fullmatch(pattern, after.get("display", ""))
    if not first or not last or first[2] != last[2]:
        return "OPEN"
    if Decimal(last[1]) > Decimal(first[1]):
        return "FAIL"
    if not item.get("zero_item_charge") or Decimal(last[1]) != Decimal(first[1]):
        return "OPEN"
    if before.get("display_resolution") != "0.01 MB" or after.get("display_resolution") != "0.01 MB":
        return "OPEN"
    # Conservative upper bound under either decimal MB or binary MiB interpretation.
    if Decimal("0.01") * 1024 * 1024 >= state["media"]["size"]:
        return "OPEN"
    try:
        uploaded = datetime.fromisoformat(state["upload_completed_at"])
        baseline = datetime.fromisoformat(before["observed_at"])
        observed = datetime.fromisoformat(after["observed_at"])
        item_at = datetime.fromisoformat(item["observed_at"])
        if not all(t.tzinfo for t in (uploaded, baseline, observed, item_at)):
            return "OPEN"
        elapsed = (observed - uploaded).total_seconds()
        item_elapsed = (item_at - uploaded).total_seconds()
        valid = baseline < uploaded and elapsed >= 86400 and item_elapsed >= 86400
        return "PASS" if valid else "OPEN"
    except (KeyError, TypeError, ValueError):
        return "OPEN"


def saved_original(root: Path, state: dict[str, Any]) -> str:
    report_path = root / "cloud" / "report.json"
    original = root / "cloud" / "cloud-original.mp4"
    if not report_path.exists() or not original.exists():
        return "OPEN"
    if original.is_symlink() or original.parent.is_symlink():
        raise CanaryBlockedError("symlink cloud artifact rejected")
    report = read_private(report_path)
    media = state["media"]
    if original.stat().st_size != media["size"] or sha256(original) != media["sha256"]:
        return "FAIL"
    if any(report.get(key) != media[key] for key in ("size", "sha256")):
        return "FAIL"
    if any(report.get("media", {}).get(key) != media[key] for key in ("width", "height", "codec", "duration")):
        return "FAIL"
    provenance = (
        report.get("original") == "PASS"
        and report.get("filename") == state["filename"]
        and report.get("independent_of_guest_source") is True
        and report.get("archive_items") == 1
        and report.get("full_decode") == "PASS"
        and report.get("used_m22") is False
        and media["width"] >= 3840
        and media["height"] >= 2160
    )
    return "PASS" if provenance else "OPEN"


def verify_existing(client: Any, state: dict[str, Any], root: Path) -> None:
    if not state.get("upload_completed_at") or not state.get("filename"):
        raise CanaryBlockedError("no completed upload checkpoint; live verification is unavailable")
    runner = client.containers.get(state["runner_id"])
    if runner.labels.get(OWNER_LABEL) != state["owner"] or "pixav.task_id" in runner.labels:
        raise CanaryBlockedError("verification runtime ownership mismatch")
    item = collect_item(hierarchy(runner, state["owner"]), state["filename"], state["media"])
    write_private(root / "item-after-24h.json", item)
    shot = runner.exec_run(["adb", "-s", "127.0.0.1:5555", "exec-out", "screencap", "-p"])
    if shot.exit_code or not shot.output.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CanaryBlockedError("item screenshot unavailable")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    screenshot = root / f"item-{stamp}.png"
    fd = os.open(screenshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(shot.output)
    state["item_after_24h"] = item
    state["item_screenshot"] = screenshot.name
    state["gates"]["upload"] = "PASS"
    state["gates"]["original"] = saved_original(root, state)
    state["gates"]["quota"] = quota_verdict(state, item)
    state["status"] = "LIVE_EVIDENCE_COLLECTED_AUTOMATION_OPEN"


def resume_existing(client: Any, state: dict[str, Any], root: Path) -> None:
    """Resume the same completed canary; uncertain UI is reviewed, never republished."""
    if not state.get("upload_completed_at") or not state.get("media_id"):
        raise CanaryBlockedError("no completed upload to reconcile")
    containers = {}
    for key, role in (("guest_id", "guest"), ("runner_id", "tools")):
        container = client.containers.get(state[key])
        if (
            container.labels.get(OWNER_LABEL) != state["owner"]
            or container.labels.get("pixav.photos_canary.role") != role
            or "pixav.task_id" in container.labels
        ):
            raise CanaryBlockedError("resume runtime ownership mismatch")
        containers[key] = container
    state["status"] = "RESUME_RECONCILIATION_PENDING"
    write_private(root / "checkpoint.json", state)
    restarted = []
    for key, container in containers.items():
        if container.status != "running":
            container.start()
            restarted.append(key)
            state.setdefault("recovery_runtime_events", []).append(
                {"container_id": container.id, "role": key, "started_at": datetime.now(timezone.utc).isoformat()}
            )
            write_private(root / "checkpoint.json", state)
    runner = containers["runner_id"]
    runner.exec_run(["adb", "connect", "127.0.0.1:5555"])
    guest = containers["guest_id"]
    ready = guest.exec_run(["getprop", "sys.boot_completed"])
    if ready.exit_code or ready.output.strip() != b"1":
        raise CanaryBlockedError("same guest is booting; resume again after readiness")
    filename = state["filename"]
    if not re.fullmatch(r"pixav-canary-[a-f0-9]{12}-2160p\.mp4", filename):
        raise CanaryBlockedError("unrecognized calibrated canary filename")
    remote = "/sdcard/DCIM/Camera/" + filename
    digest = guest.exec_run(["sha256sum", remote])
    if digest.exit_code or digest.output.decode().split()[0] != state["media"]["sha256"]:
        raise CanaryBlockedError("existing guest original hash mismatch; do not reupload")
    # Filename is constrained above; SQL and shell arguments cannot contain user syntax.
    query = runner.exec_run(
        [
            "adb",
            "-s",
            "127.0.0.1:5555",
            "shell",
            "content query --uri content://media/external/video/media "
            "--projection _id:_display_name:_size "
            f'''--where "_display_name='{filename}'"''',
        ]
    )
    rows = query.output.decode().splitlines()
    if (
        query.exit_code
        or len(rows) != 1
        or not re.search(rf"\b_id={re.escape(str(state['media_id']))}(?:,|$)", rows[0])
        or f"_display_name={filename}" not in rows[0]
        or not re.search(rf"\b_size={state['media']['size']}(?:,|$)", rows[0])
    ):
        raise CanaryBlockedError("existing MediaStore item is ambiguous; do not reupload")
    verify_existing(client, state, root)
    recovery = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "result": "PASS",
        "guest_id": state["guest_id"],
        "runner_id": state["runner_id"],
        "started_existing_containers": restarted,
        "same_media_id": state["media_id"],
        "matching_media_store_rows": 1,
        "guest_sha256_match": True,
        "cloud_original_rehashed": state["gates"]["original"] == "PASS",
        "credential_submissions": 0,
        "media_publications": 0,
        "sharing_mutations": 0,
    }
    state.setdefault("recovery_observations", []).append(recovery)
    write_private(root / "recovery.json", recovery)
