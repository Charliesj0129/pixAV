"""A single confirmed-part guest/tools restart drill, invoked only by the CLI."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pixav.pixel_injector.canary import OWNER_LABEL, CanaryBlockedError
from pixav.pixel_injector.maestro_parts import MaestroPartUploader


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def snapshot(parts: list) -> str:
    keys = {
        "video_id",
        "part_index",
        "manifest_version",
        "sha256",
        "size_bytes",
        "filename",
        "share_url",
        "account_id",
        "uploaded_at",
        "usage_counted_at",
        "recovery",
    }
    return digest([p.model_dump(mode="json", include=keys) for p in parts if p.usage_counted_at is not None])


async def retained(flow: Any, work: Path) -> tuple[Any, Any]:
    """Inspect exact recorded IDs, mounts and network; never create or start."""
    runtime = flow.state.get("runtime", {})
    items = []
    for role in ("guest", "tools"):
        if not runtime.get(role + "_id"):
            raise CanaryBlockedError("drill requires retained guest and tools IDs")
        item = await asyncio.to_thread(flow.client.containers.get, runtime[role + "_id"])
        if (
            item.labels.get(OWNER_LABEL) != str(flow.id)
            or item.labels.get("pixav.photos_canary.role") != role
            or "pixav.task_id" in item.labels
        ):
            raise CanaryBlockedError("drill ownership mismatch")
        config = flow.state.get("configuration", {}).get("runtime", {})
        if config:
            image = config["profile"]["image"] if role == "guest" else "pixav-photos-canary:maestro-2.10.0"
            if item.attrs.get("Image") != config["images"][image]:
                raise CanaryBlockedError("retained runtime image changed")
        destination = "/data" if role == "guest" else "/parts"
        source = work / "guest" if role == "guest" else work / "parts" / flow.state["video_id"]
        mounts = [m for m in item.attrs["Mounts"] if m["Destination"] == destination]
        if len(mounts) != 1 or Path(mounts[0]["Source"]) != source or mounts[0].get("RW") != (role == "guest"):
            raise CanaryBlockedError("drill mount mismatch")
        if any(m.get("Type") == "bind" and m["Destination"] != destination for m in item.attrs["Mounts"]):
            raise CanaryBlockedError("unexpected retained runtime bind mount")
        items.append(item)
    if items[1].attrs["HostConfig"]["NetworkMode"] != "container:" + items[0].id:
        raise CanaryBlockedError("retained tools namespace changed")
    return items[0], items[1]


async def drill(flow: Any, work: Path, root: Path) -> dict:  # noqa: C901 - explicit safe-boundary checks
    import uuid

    if flow.args.run_id is None or str(flow.args.run_id) != str(flow.id):
        raise CanaryBlockedError("recovery-drill requires exact --run-id")
    parts = await flow.parts.list(uuid.UUID(flow.state["video_id"]))
    confirmed = [p for p in parts if p.usage_counted_at is not None]
    if (
        flow.state["stage"] != "upload_paused"
        or len(confirmed) != 1
        or confirmed[0].part_index != 0
        or len(parts) <= 1
        or flow.state.get("recovery_drill")
        or any(p.recovery or p.state != "prepared" for p in parts if p.usage_counted_at is None)
    ):
        raise CanaryBlockedError(
            "drill requires first part confirmed and untouched pending parts; prior drill cannot be retried"
        )
    if not confirmed[0].recovery.get("media_id") or not confirmed[0].share_url:
        raise CanaryBlockedError("confirmed part has no MediaStore/share identity")
    guest, runner = await retained(flow, work)
    if any(item.status != "running" for item in (guest, runner)):
        raise CanaryBlockedError("drill requires running containers at the safe boundary")
    before = {role: item.attrs["State"]["StartedAt"] for role, item in (("guest", guest), ("tools", runner))}
    record = {
        "intent_at": stamp(),
        "invocation_id": flow.state.get("invocation_id"),
        "confirmed_snapshot": snapshot(confirmed),
        "guest_id": guest.id,
        "tools_id": runner.id,
        "started_before": before,
        "device_snapshot": digest(flow.state.get("device", {})),
    }
    flow.state["recovery_drill"] = record
    await flow.save()
    for item in (runner, guest):
        flow.check_operation()
        await asyncio.to_thread(item.stop, timeout=10)
    for item in (guest, runner):
        flow.check_operation()
        await asyncio.to_thread(item.start)
    guest, runner = await retained(flow, work)
    after = {role: item.attrs["State"]["StartedAt"] for role, item in (("guest", guest), ("tools", runner))}
    if any(before[role] == after[role] for role in before):
        raise CanaryBlockedError("actual runtime restart not observed")
    for _ in range(120):
        result = await asyncio.to_thread(guest.exec_run, ["getprop", "sys.boot_completed"])
        if result.exit_code == 0 and result.output.strip() == b"1":
            break
        await asyncio.sleep(2)
    else:
        raise CanaryBlockedError("restarted guest boot deadline exceeded")

    async def no_write(_value: dict) -> None:
        raise CanaryBlockedError("drill attempted to modify confirmed recovery")

    part = confirmed[0]
    uploader = MaestroPartUploader(
        guest, runner, str(flow.id), part, dict(part.recovery), no_write, root / "config/maestro/photos-canary"
    )
    uploader.check = flow.check_operation
    await uploader.adb("connect", "127.0.0.1:5555")
    await uploader._media_store("/sdcard/DCIM/Camera/" + part.filename, readonly=True)
    if snapshot(await flow.parts.list(part.video_id)) != record["confirmed_snapshot"]:
        raise CanaryBlockedError("confirmed part changed during drill")
    record.update(completed_at=stamp(), started_after=after, identity_verified=True)
    await flow.save()
    return {"status": "RECOVERY_DRILL_COMPLETE", "run_id": str(flow.id), "automation": "OPEN"}


async def reconcile(flow: Any, parts: list, work: Path) -> None:
    record = flow.state.get("recovery_drill")
    if not record:
        return
    if not record.get("completed_at") or not record.get("identity_verified"):
        raise CanaryBlockedError("incomplete recovery drill; retain runtime")
    if snapshot([p for p in parts if p.part_index == 0]) != record["confirmed_snapshot"]:
        raise CanaryBlockedError("confirmed part changed after restart")
    guest, runner = await retained(flow, work)
    if guest.id != record["guest_id"] or runner.id != record["tools_id"]:
        raise CanaryBlockedError("drill runtime identity changed")
    if not record.get("resumed_at"):
        if digest(flow.state.get("device", {})) != record["device_snapshot"]:
            raise CanaryBlockedError("device recovery changed after restart")
        record.update(
            resumed_at=stamp(),
            resumed_invocation_id=flow.state.get("invocation_id"),
            skipped_confirmed_parts=[0],
            no_duplicate_effects=True,
        )
        await flow.save()


def automation_verdict(state: dict, parts: list) -> str:
    record = state.get("recovery_drill", {})
    if not (record.get("completed_at") and record.get("resumed_at") and record.get("no_duplicate_effects")):
        return "OPEN"
    ordered = state.get("cli_invocations", [])
    invocations = {item["id"]: item for item in ordered}
    restart = invocations.get(record.get("invocation_id"), {})
    resumed = invocations.get(record.get("resumed_invocation_id"), {})
    if (
        restart.get("command") != "recovery-drill"
        or restart.get("result") != "RECOVERY_DRILL_COMPLETE"
        or resumed.get("command") != "resume"
    ):
        return "OPEN"
    positions = {item["id"]: index for index, item in enumerate(ordered)}
    resumed_index = positions[record["resumed_invocation_id"]]
    # Quota/cooldown can suspend the first post-drill resume. Completion may
    # legitimately occur in a later invocation; pre-drill receipts cannot count.
    if resumed_index <= positions[record["invocation_id"]] or not any(
        item.get("command") == "resume" and item.get("uploads_completed_at") for item in ordered[resumed_index:]
    ):
        return "OPEN"
    if len(parts) < 2 or any(p.usage_counted_at is None for p in parts):
        return "OPEN"
    for part in parts:
        for kind in ("push", "publish", "backup", "share"):
            operation = part.recovery.get("operations", {}).get(kind, {})
            if not all(operation.get(k) for k in ("intent_at", "completed_at", "identity_verified")):
                return "OPEN"
    return "PASS"
