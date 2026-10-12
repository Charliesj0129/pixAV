"""Read-only capacity verdicts from DB-clock UI evidence, separate from quota."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal


def _time(value: str) -> datetime:
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError("capacity time must be timezone aware")
    return at


def _capacity(value: dict) -> tuple[Decimal, str]:
    match = re.fullmatch(r"([0-9]+\.[0-9]{2}) MB of ([0-9]+) GB", value.get("display", ""))
    if match is None or value.get("display_resolution") != "0.01 MB":
        raise ValueError("provider capacity precision unsupported")
    return Decimal(match[1]), match[2]


def _sample_interval(samples: list[dict], baseline: dict, confirmed_at: datetime, immutable_set: dict) -> dict:
    baseline_capacity = _capacity(baseline)
    baseline_at = _time(baseline["observed_at"])
    if baseline.get("clock") != "postgresql" or baseline_at >= confirmed_at:
        raise ValueError("capacity requires original pre-upload DB-clock baseline")
    binding = baseline["binding"]
    start = prior = None
    interruptions = []
    for sample in samples:
        at = _validate_sample(sample, baseline_capacity, baseline_at, binding, immutable_set)
        if prior is not None:
            if at <= prior:
                raise ValueError("non-monotonic capacity DB clock")
            if (at - prior).total_seconds() > 900:
                interruptions.append({"before": prior.isoformat(), "after": at.isoformat()})
                start = at
        if start is None:
            start = at
        prior = at
    if start is None or prior is None:
        return {"continuous_seconds": 0, "interruptions": []}
    valid_start = max(start, confirmed_at)
    return {
        "continuous_start": valid_start.isoformat(),
        "continuous_seconds": max(0, (prior - valid_start).total_seconds()),
        "interruptions": interruptions,
        "latest_at": prior.isoformat(),
    }


def _validate_sample(
    sample: dict, capacity: tuple[Decimal, str], baseline_at: datetime, binding: dict, immutable_set: dict
) -> datetime:
    at = _time(sample["observed_at"])
    started = _time(sample["observation_started_at"])
    finished = _time(sample["observation_finished_at"])
    if sample.get("clock") != "postgresql" or not started <= at <= finished or at <= baseline_at:
        raise ValueError("capacity sample DB clock invalid")
    if sample.get("binding") != binding or sample.get("immutable_upload_set") != immutable_set:
        raise ValueError("capacity account or immutable upload/provider/usage set changed")
    if _capacity(sample) != capacity or sample.get("external_activity") != "none-confirmed":
        raise ValueError("capacity or external activity requires independent attribution")
    if not sample.get("backup_complete") or not sample.get("original_quality_confirmed"):
        raise ValueError("original quality or backup status unconfirmed")
    return at


def _items_complete(reviews: dict, expected: dict, interval: dict, latest: dict) -> bool:
    if set(reviews) != set(expected):
        return False
    end = _time(interval["continuous_start"]) + timedelta(hours=24)
    for key, item in reviews.items():
        if not all(item.get(k) for k in ("backed_up", "original_quality", "zero_item_charge")):
            return False
        if not end <= _time(item["observed_at"]) <= _time(latest["observation_finished_at"]):
            return False
        if item.get("source_sha256") != expected[key]["sha256"] or expected[key]["size_bytes"] <= 10485.76:
            return False
        if item.get("binding") != latest["binding"] or item.get("clock") != "postgresql":
            return False
    return True


def capacity_state(
    samples: list[dict],
    baseline: dict,
    last_confirmed_at: str | None,
    expected: dict,
    item_reviews: dict,
    immutable_set: dict,
    *,
    uploads_resolved: bool,
    ui_owned: bool,
    all_cold_durable: bool,
) -> dict:
    """Upload reconciliation gates sampling; cold durability gates completion.

    Collection owns no execution transitions. A gap over 900 seconds restarts
    the interval. Even a full interval never substitutes for per-item review.
    """
    if not uploads_resolved or last_confirmed_at is None:
        return {"state": "WAITING_UPLOAD_RECONCILIATION", "reason": "UPLOAD_OUTCOME_UNRESOLVED"}
    if not ui_owned:
        return {"state": "WAITING_UI_OWNERSHIP", "reason": "UI_READER_OR_WRITER_ACTIVE"}
    try:
        interval = _sample_interval(samples, baseline, _time(last_confirmed_at), immutable_set)
        if not samples or interval["continuous_seconds"] < 86400:
            state = (
                "GAP_RESTARTED"
                if interval["interruptions"] and interval.get("latest_at") == interval.get("continuous_start")
                else "OBSERVING"
            )
            return {"state": state, "reason": "CONTINUOUS_24H_INCOMPLETE", **interval}
        if not all_cold_durable or not _items_complete(item_reviews, expected, interval, samples[-1]):
            return {
                "state": "CAPACITY_REVIEW_REQUIRED",
                "reason": "COLD_DURABILITY_OR_ITEM_REVIEW_INCOMPLETE",
                **interval,
            }
        return {"state": "COMPLETE", "reason": "CONTINUOUS_24H_AND_ALL_ITEMS_CONFIRMED", **interval}
    except (ValueError, KeyError, TypeError):
        return {"state": "CAPACITY_REVIEW_REQUIRED", "reason": "INVALID_EVIDENCE_OR_UNATTRIBUTED_ACCOUNT_ACTIVITY"}
