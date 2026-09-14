"""Reject insufficient or mismatched live canary evidence."""

import hashlib

import pytest

from pixav.pixel_injector.canary import CanaryBlockedError, write_private
from pixav.pixel_injector.canary_acceptance import collect_item, quota_verdict, saved_original


@pytest.fixture
def state():
    return {
        "filename": "canary.mp4",
        "media": {
            "size": 128742951,
            "sha256": "a" * 64,
            "width": 3840,
            "height": 2160,
            "codec": "h264",
            "duration": 100.0,
        },
        "upload_completed_at": "2026-09-09T01:00:00+00:00",
        "quota_before": {
            "display": "124.88 MB of 15 GB",
            "display_resolution": "0.01 MB",
            "observed_at": "2026-09-09T00:59:00+00:00",
        },
        "quota_after_24h": {
            "display": "124.88 MB of 15 GB",
            "display_resolution": "0.01 MB",
            "observed_at": "2026-09-10T01:00:00+00:00",
        },
    }


def test_exact_item_and_explicit_charge(state):
    texts = [
        "canary.mp4",
        "Backed up",
        "Original quality",
        "3840 x 2160",
        "This item doesn't take up space in your account storage. Learn more",
    ]
    item = collect_item([{"text": t} for t in texts], state["filename"], state["media"])
    assert item["zero_item_charge"] is True
    for missing in texts[:4]:
        with pytest.raises(CanaryBlockedError):
            collect_item([{"text": t} for t in texts if t != missing], state["filename"], state["media"])


@pytest.mark.parametrize(
    "change,expected",
    [
        (None, "PASS"),
        ("increase", "FAIL"),
        ("early", "OPEN"),
        ("precision", "OPEN"),
        ("no_item_charge", "OPEN"),
        ("rounded", "OPEN"),
        ("naive_time", "OPEN"),
    ],
)
def test_quota_evidence(state, change, expected):
    item = {"zero_item_charge": True, "observed_at": "2026-09-10T01:01:00+00:00"}
    after = state["quota_after_24h"]
    if change == "increase":
        after["display"] = "124.89 MB of 15 GB"
    if change == "early":
        after["observed_at"] = "2026-09-10T00:59:59+00:00"
    if change == "precision":
        after["display_resolution"] = "1 GB"
    if change == "no_item_charge":
        item["zero_item_charge"] = False
    if change == "rounded":
        after["display"] = "0% of 15 GB used"
    if change == "naive_time":
        after["observed_at"] = "2026-09-10T01:00:00"
    assert quota_verdict(state, item) == expected


def test_original_requires_artifact_and_provenance(tmp_path, state):
    assert saved_original(tmp_path, state) == "OPEN"
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    data = b"cloud original"
    state["media"].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    (cloud / "cloud-original.mp4").write_bytes(data)
    report = {
        **state["media"],
        "media": state["media"],
        "filename": state["filename"],
        "original": "PASS",
        "independent_of_guest_source": True,
        "archive_items": 1,
        "full_decode": "PASS",
        "used_m22": False,
    }
    write_private(cloud / "report.json", report)
    assert saved_original(tmp_path, state) == "PASS"
    report["used_m22"] = True
    write_private(cloud / "report.json", report)
    assert saved_original(tmp_path, state) == "OPEN"
    (cloud / "cloud-original.mp4").write_bytes(b"corrupt")
    assert saved_original(tmp_path, state) == "FAIL"


def test_resume_reuses_ids_and_never_republishes(tmp_path, state, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from pixav.pixel_injector.canary import OWNER_LABEL
    from pixav.pixel_injector.canary_acceptance import resume_existing

    state.update(
        owner="owner",
        guest_id="guest",
        runner_id="tools",
        media_id="19",
        filename="pixav-canary-aaaaaaaaaaaa-2160p.mp4",
        gates={"original": "PASS"},
    )
    guest = Mock(status="running", labels={OWNER_LABEL: "owner", "pixav.photos_canary.role": "guest"})
    runner = Mock(id="tools", status="exited", labels={OWNER_LABEL: "owner", "pixav.photos_canary.role": "tools"})
    guest.exec_run.side_effect = [
        SimpleNamespace(exit_code=0, output=b"1"),
        SimpleNamespace(exit_code=0, output=(state["media"]["sha256"] + "  source.mp4").encode()),
    ]
    runner.exec_run.side_effect = [
        SimpleNamespace(exit_code=0, output=b"connected"),
        SimpleNamespace(
            exit_code=0, output=b"Row: 0 _id=19, _display_name=pixav-canary-aaaaaaaaaaaa-2160p.mp4, _size=128742951\n"
        ),
    ]
    client = Mock()
    client.containers.get.side_effect = lambda key: {"guest": guest, "tools": runner}[key]
    verifier = Mock()
    monkeypatch.setattr("pixav.pixel_injector.canary_acceptance.verify_existing", verifier)
    resume_existing(client, state, tmp_path)
    runner.start.assert_called_once()
    guest.start.assert_not_called()
    client.containers.run.assert_not_called()
    verifier.assert_called_once_with(client, state, tmp_path)
    assert state["recovery_observations"][0]["credential_submissions"] == 0
    assert state["recovery_observations"][0]["media_publications"] == 0
    assert all("scan_file" not in str(call) for call in guest.exec_run.call_args_list)


def test_resume_ownership_checked_before_start(tmp_path, state):
    from unittest.mock import Mock

    from pixav.pixel_injector.canary_acceptance import resume_existing

    state.update(owner="owner", guest_id="guest", runner_id="tools", media_id="19")
    client = Mock()
    client.containers.get.return_value.labels = {}
    with pytest.raises(CanaryBlockedError, match="ownership"):
        resume_existing(client, state, tmp_path)
    client.containers.get.return_value.start.assert_not_called()
