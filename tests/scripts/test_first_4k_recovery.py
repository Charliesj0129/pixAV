"""Simulated safe-boundary contracts; these are not live guest restart proof."""

import copy
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pixav.pixel_injector.canary import OWNER_LABEL, CanaryBlockedError
from pixav.shared.models import VideoPart
from scripts import first_4k_recovery as recovery


@pytest.fixture
def boundary(tmp_path):
    run, video = uuid.uuid4(), uuid.uuid4()
    timestamp = datetime.now(timezone.utc)
    parts = [
        VideoPart(
            video_id=video,
            part_index=i,
            manifest_version=1,
            start_seconds=i * 10,
            end_seconds=(i + 1) * 10,
            size_bytes=1000,
            sha256="a" * 64,
            filename=f"pixav-{video}-part-{i:06d}-{'a' * 16}.mp4",
            usage_counted_at=timestamp if i == 0 else None,
            uploaded_at=timestamp if i == 0 else None,
            recovery={"media_id": "42"} if i == 0 else {},
            share_url="https://photos.app.goo.gl/fixture" if i == 0 else None,
        )
        for i in range(2)
    ]
    events = []

    class Container:
        def __init__(self, role):
            self.id = role
            self.labels = {OWNER_LABEL: str(run), "pixav.photos_canary.role": role}
            self.status = "running"
            self.attrs = {
                "State": {"StartedAt": "before"},
                "HostConfig": {"NetworkMode": "container:guest"},
                "Mounts": [
                    {
                        "Destination": "/data" if role == "guest" else "/parts",
                        "RW": role == "guest",
                        "Source": str(tmp_path / "guest" if role == "guest" else tmp_path / "parts" / str(video)),
                    }
                ],
            }

        def stop(self, **kwargs):
            events.append((self.id, "stop"))
            self.status = "exited"

        def start(self):
            events.append((self.id, "start"))
            self.status = "running"
            self.attrs["State"]["StartedAt"] = "after"

        def exec_run(self, _cmd):
            return SimpleNamespace(exit_code=0, output=b"1")

    containers = {role: Container(role) for role in ("guest", "tools")}
    state = {
        "id": str(run),
        "video_id": str(video),
        "stage": "upload_paused",
        "invocation_id": "drill-cli",
        "runtime": {"guest_id": "guest", "tools_id": "tools"},
    }
    flow = SimpleNamespace(
        id=run,
        args=SimpleNamespace(run_id=run),
        state=state,
        client=SimpleNamespace(containers=SimpleNamespace(get=containers.__getitem__)),
        parts=SimpleNamespace(list=AsyncMock(return_value=parts)),
        save=AsyncMock(),
        check_operation=lambda: None,
    )
    return flow, parts, containers, events, tmp_path


async def test_drill_requires_matching_explicit_run(boundary):
    flow, _, _, events, root = boundary
    flow.args.run_id = None
    with pytest.raises(CanaryBlockedError, match="exact"):
        await recovery.drill(flow, root, root)
    assert events == []
    flow.save.assert_not_awaited()


@pytest.mark.parametrize("corrupt", ["owner", "mount", "namespace", "pending", "multiple"])
async def test_unsafe_drill_has_no_effects(boundary, corrupt):
    flow, parts, containers, events, root = boundary
    if corrupt == "owner":
        containers["guest"].labels[OWNER_LABEL] = "another-run"
    elif corrupt == "mount":
        containers["guest"].attrs["Mounts"][0]["Source"] = "/another"
    elif corrupt == "namespace":
        containers["tools"].attrs["HostConfig"]["NetworkMode"] = "host"
    elif corrupt == "pending":
        parts[1].recovery["push_intent"] = True
    else:
        parts[1] = parts[1].model_copy(update={"usage_counted_at": parts[0].usage_counted_at})
    with pytest.raises(CanaryBlockedError):
        await recovery.drill(flow, root, root)
    assert events == []
    flow.save.assert_not_awaited()


async def test_drill_and_resume_preserve_confirmed_identity(boundary, monkeypatch):
    flow, parts, _, events, root = boundary
    uploader = SimpleNamespace(adb=AsyncMock(), _media_store=AsyncMock())
    monkeypatch.setattr(recovery, "MaestroPartUploader", lambda *_a: uploader)
    original = copy.deepcopy(parts[0])
    assert (await recovery.drill(flow, root, root))["status"] == "RECOVERY_DRILL_COMPLETE"
    assert events == [("tools", "stop"), ("guest", "stop"), ("guest", "start"), ("tools", "start")]
    uploader._media_store.assert_awaited_once_with("/sdcard/DCIM/Camera/" + parts[0].filename, readonly=True)
    assert parts[0] == original
    flow.state["invocation_id"] = "resume-cli"
    await recovery.reconcile(flow, parts, root)
    assert flow.state["recovery_drill"]["skipped_confirmed_parts"] == [0]
    # Later cold verification is allowed to enrich verification/state. Upload
    # effects, IDs and debit timestamps must remain immutable across resumes.
    parts[0] = parts[0].model_copy(update={"state": "verified"})
    parts[0].verification["original"] = {"sha256": parts[0].sha256}
    await recovery.reconcile(flow, parts, root)
    parts[0] = parts[0].model_copy(update={"share_url": "https://photos.app.goo.gl/changed"})
    with pytest.raises(CanaryBlockedError, match="changed"):
        await recovery.reconcile(flow, parts, root)
    with pytest.raises(CanaryBlockedError, match="prior drill"):
        await recovery.drill(flow, root, root)


def test_handwritten_gate_or_incomplete_receipts_never_pass(boundary):
    flow, parts, _, _, _ = boundary
    flow.state["gates"] = {"automation": "PASS"}
    assert recovery.automation_verdict(flow.state, parts) == "OPEN"
    flow.state["recovery_drill"] = {"completed_at": "now", "resumed_at": "now", "no_duplicate_effects": True}
    assert recovery.automation_verdict(flow.state, parts) == "OPEN"


@pytest.mark.parametrize("completion_after_drill", [False, True])
def test_automation_accepts_completion_after_multiple_quota_resumes(boundary, completion_after_drill):
    flow, parts, _, _, _ = boundary
    timestamp = datetime.now(timezone.utc).isoformat()
    parts = [
        part.model_copy(
            update={
                "usage_counted_at": parts[0].usage_counted_at,
                "recovery": {
                    "operations": {
                        kind: {"intent_at": timestamp, "completed_at": timestamp, "identity_verified": True}
                        for kind in ("push", "publish", "backup", "share")
                    }
                },
            }
        )
        for part in parts
    ]
    flow.state["recovery_drill"] = {
        "completed_at": timestamp,
        "resumed_at": timestamp,
        "no_duplicate_effects": True,
        "invocation_id": "restart",
        "resumed_invocation_id": "first-resume",
    }
    attempts = [
        {"id": "restart", "command": "recovery-drill", "result": "RECOVERY_DRILL_COMPLETE"},
        {"id": "first-resume", "command": "resume", "result": "QUOTA_WAIT"},
        {"id": "second-resume", "command": "resume", "result": "QUOTA_WAIT"},
    ]
    completed = {"id": "finished", "command": "resume", "uploads_completed_at": timestamp}
    flow.state["cli_invocations"] = attempts
    assert recovery.automation_verdict(flow.state, parts) == "OPEN"
    attempts.insert(len(attempts) if completion_after_drill else 0, completed)
    assert recovery.automation_verdict(flow.state, parts) == ("PASS" if completion_after_drill else "OPEN")
