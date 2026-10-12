"""UI capacity evidence cannot borrow elapsed time from uploads or old gaps."""

from datetime import datetime, timedelta, timezone

import pytest

from pixav.shared.capacity_observation import capacity_state

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
BINDING = {"account_id": "synthetic", "owner": "synthetic", "profile": "gphotos_pixel_xl_v1"}
SET = {"uploads": ["original-operation"], "markers": ["unique-marker"], "providers": ["exact-item"]}
BASELINE = {
    "observed_at": (START - timedelta(minutes=5)).isoformat(),
    "display": "10.00 MB of 15 GB",
    "display_resolution": "0.01 MB",
    "clock": "postgresql",
    "binding": BINDING,
}
EXPECTED = {"segment": {"sha256": "a" * 64, "size_bytes": 20000}}


def sample(seconds):
    at = (START + timedelta(seconds=seconds)).isoformat()
    return {
        **BASELINE,
        "observed_at": at,
        "observation_started_at": at,
        "observation_finished_at": at,
        "immutable_upload_set": SET,
        "original_quality_confirmed": True,
        "backup_complete": True,
        "external_activity": "none-confirmed",
    }


def verdict(samples, **changes):
    args = dict(
        samples=samples,
        baseline=BASELINE,
        last_confirmed_at=START.isoformat(),
        expected=EXPECTED,
        item_reviews={},
        immutable_set=SET,
        uploads_resolved=True,
        ui_owned=True,
        all_cold_durable=False,
    )
    return capacity_state(**{**args, **changes})


def test_reconciliation_then_shared_ui_ownership_gate_sampling():
    assert verdict([], uploads_resolved=False)["state"] == "WAITING_UPLOAD_RECONCILIATION"
    assert verdict([], ui_owned=False)["state"] == "WAITING_UI_OWNERSHIP"
    assert verdict([])["state"] == "OBSERVING"
    assert verdict([sample(0), sample(300)])["continuous_seconds"] == 300


def test_gap_901_restarts_but_900_preserves_interval():
    assert verdict([sample(0), sample(900)])["continuous_seconds"] == 900
    result = verdict([sample(0), sample(901)])
    assert result["state"] == "GAP_RESTARTED"
    assert result["continuous_seconds"] == 0


def test_start_is_later_than_unknown_outcome_confirmation():
    result = verdict(
        [sample(0), sample(300), sample(600)], last_confirmed_at=(START + timedelta(seconds=500)).isoformat()
    )
    assert result["continuous_seconds"] == 100


@pytest.mark.parametrize(
    "change",
    [
        {"binding": {"account_id": "other"}},
        {"immutable_upload_set": {}},
        {"display": "10.01 MB of 15 GB"},
        {"display_resolution": "1 MB"},
        {"external_activity": "unattributed-drive-backup"},
        {"backup_complete": False},
        {"original_quality_confirmed": False},
        {"observed_at": "2026-01-01T00:00:00"},
        {"clock": "host"},
    ],
)
def test_unattributed_activity_precision_or_changed_binding_stops_acceptance(change):
    assert verdict([{**sample(0), **change}])["state"] == "CAPACITY_REVIEW_REQUIRED"


def test_full_interval_waits_for_cold_durability_and_exact_after_window_items():
    samples = [sample(t) for t in range(0, 86401, 300)]
    assert verdict(samples)["state"] == "CAPACITY_REVIEW_REQUIRED"
    review = {
        "segment": {
            "backed_up": True,
            "original_quality": True,
            "zero_item_charge": True,
            "source_sha256": "a" * 64,
            "observed_at": samples[-1]["observed_at"],
            "binding": BINDING,
            "clock": "postgresql",
        }
    }
    assert verdict(samples, item_reviews=review, all_cold_durable=True)["state"] == "COMPLETE"
    review["segment"]["observed_at"] = samples[-2]["observed_at"]
    assert verdict(samples, item_reviews=review, all_cold_durable=True)["state"] == "CAPACITY_REVIEW_REQUIRED"


def test_nonmonotonic_time_and_old_baseline_cannot_pass():
    assert verdict([sample(300), sample(0)])["state"] == "CAPACITY_REVIEW_REQUIRED"
    assert (
        verdict([sample(0)], baseline={**BASELINE, "observed_at": START.isoformat()})["state"]
        == "CAPACITY_REVIEW_REQUIRED"
    )
