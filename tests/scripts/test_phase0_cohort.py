"""Timing evidence cannot turn incomplete work into a passing runtime gate."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from pixav.shared.exceptions import SourceUnavailableError
from pixav.shared.phase0_timing import phase0_span
from scripts.phase0_cohort import build_report, percentiles, write_json


def observation(at, tasks):
    return {
        "at": at.isoformat(),
        "database_identity": "cluster",
        "database": "test",
        "redis_run_id": "run",
        "tasks": tasks,
        "queues": {"download": {"queued": [], "processing": []}},
    }


def test_wall_clock_includes_operator_gaps_not_sum_of_task_times():
    start = datetime(2026, 9, 7, tzinfo=timezone.utc)
    tasks = [{"task_id": str(i), "state": "dispatched", "retries": 0, "retry_not_before": None} for i in range(3)]
    cohort = observation(start, tasks)
    final = observation(
        start + timedelta(seconds=1000),
        [dict(task, state="failed", failure_class="SourceUnavailableError") for task in tasks],
    )
    events = []
    for i in range(3):
        common = {"task_id": str(i), "stage": "download", "span_id": str(i)}
        events.extend(
            [
                dict(common, event="start", at=(start + timedelta(seconds=i * 10)).isoformat()),
                dict(
                    common,
                    event="end",
                    at=(start + timedelta(seconds=i * 10 + 5)).isoformat(),
                    elapsed_seconds=5,
                    outcome="complete",
                ),
            ]
        )
    events.append({"event": "operator_interruption", "at": (start + timedelta(seconds=30)).isoformat()})
    report = build_report(cohort, [final], events, {"status": "unavailable"})
    assert report["cohort_size"] == 3
    assert report["wall_clock_seconds"] == 1000
    assert report["stage_seconds"]["download"] == {"n": 3, "p50": 5, "p95": 5}
    assert report["operator_markers"]
    assert report["production_gate"] == "OPEN"
    assert report["missing_stage_evidence"] == ["remux", "local_finalize"]


def test_missing_task_open_span_and_instance_change():
    at = datetime.now(timezone.utc)
    cohort = observation(at, [{"task_id": "one", "state": "downloading", "retry_not_before": None}])
    final = observation(at + timedelta(seconds=20), [])
    event = {"task_id": "one", "event": "start", "stage": "download", "span_id": "crashed", "at": at.isoformat()}
    report = build_report(cohort, [final], [event], {"status": "unavailable"})
    assert report["unfinished_task_ids"] == ["one"]
    assert report["open_span_ids"] == ["crashed"]
    with pytest.raises(ValueError, match="instance changed"):
        build_report(cohort, [dict(final, redis_run_id="restarted")], [], {})


def test_span_redacts_failure_and_records_interruption(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXAV_PHASE0_EVENTS_DIR", str(tmp_path))
    task, video = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(SourceUnavailableError):
        with phase0_span(task, video, "download"):
            raise SourceUnavailableError("secret-cookie=do-not-save")
    with pytest.raises(KeyboardInterrupt):
        with phase0_span(task, video, "remux"):
            raise KeyboardInterrupt()
    events = [json.loads(path.read_text()) for path in tmp_path.glob("*.json")]
    assert len(events) == 4
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in tmp_path.glob("*.json"))
    assert "secret-cookie" not in json.dumps(events)
    assert any(e.get("failure_class") == "SourceUnavailableError" for e in events)
    assert any(e.get("outcome") == "interrupted" for e in events)


def test_snapshot_never_overwrites_and_empty_percentiles(tmp_path):
    path = tmp_path / "cohort.json"
    write_json(path, {"tasks": [1]})
    with pytest.raises(FileExistsError):
        write_json(path, {"tasks": [2]})
    assert json.loads(path.read_text()) == {"tasks": [1]}
    assert percentiles([]) == {"n": 0, "p50": None, "p95": None}


def test_restart_preserves_cohort_wall_clock_and_failed_stage_duration():
    at = datetime(2026, 9, 7, tzinfo=timezone.utc)
    task = {"task_id": "one", "state": "downloading", "retry_not_before": None}
    cohort = observation(at, [task])
    final = observation(at + timedelta(seconds=600), [dict(task, state="failed")])
    final.update(redis_run_id="new", redis_restart_from="run")
    events = [
        {"task_id": "one", "stage": "download", "span_id": "attempt", "event": "start", "at": at.isoformat()},
        {
            "task_id": "one",
            "stage": "download",
            "span_id": "attempt",
            "event": "end",
            "at": final["at"],
            "outcome": "failed",
            "elapsed_seconds": 300,
            "failure_class": "SourceUnavailableError",
        },
    ]
    report = build_report(cohort, [final], events, {})
    assert report["wall_clock_seconds"] == 600
    assert report["stage_seconds"]["download"]["p95"] == 300
    assert report["redis_restarts"][0]["to"] == "new"
    assert report["first_complete_observation_at"] == final["at"]


def test_complete_projection_without_available_video_is_unfinished():
    at = datetime.now(timezone.utc)
    task = {"task_id": "one", "state": "complete", "video_status": "uploading", "retry_not_before": None}
    report = build_report(observation(at, [task]), [observation(at, [task])], [], {})
    assert report["unfinished_task_ids"] == ["one"]


async def test_managed_sample_correlates_operations_and_never_reads_payloads():
    from unittest.mock import AsyncMock

    from scripts.phase0_cohort import managed_sample

    task, operation = uuid.uuid4(), uuid.uuid4()
    at = datetime.now(timezone.utc)
    pool = AsyncMock()
    pool.fetch.side_effect = [[], [], [{"operation_id": operation, "started_at": at}], [], [], []]
    result = await managed_sample(pool, [task])
    assert result["attempts"] == [{"operation_id": str(operation), "started_at": at.isoformat()}]
    for call in pool.fetch.call_args_list:
        sql, selected = call.args
        assert selected == [task]
        assert "SELECT *" not in sql
        assert not any(secret in sql for secret in ("password", "email", "share_url"))
        if "checkpoint" in sql:
            assert "e.checkpoint," not in sql
            assert "download_last_checked_at" in sql
            assert "completion_to_prepare_seconds" in sql
        if "recovery" in sql:
            assert "s.recovery#>>'{operations,backup,intent_at}' AS backup_intent_at" in sql
            assert sql.count("recovery") == 1


def test_managed_success_does_not_promote_without_live_evidence():
    at = datetime.now(timezone.utc)
    final = observation(at, [])
    final["managed"] = {"assets": [{"state": "DURABLE", "policy_version": "photos-original-v1"}]}
    report = build_report(final, [final], [], {})
    assert report["managed"] == final["managed"]
    assert report["photos_acceptance"] == report["production_gate"] == "OPEN"


def test_managed_window_uses_last_upload_and_distinct_v2_videos():
    from scripts.phase0_cohort import managed_window

    at = datetime(2026, 9, 19, tzinfo=timezone.utc)
    asset = {"video_id": "one", "state": "DURABLE", "policy_version": "photos-original-v2", "segment_count": 2}
    sample = observation(at, [])
    sample["managed"] = {
        "assets": [asset, asset, dict(asset, video_id="old", policy_version="photos-original-v1")],
        "segments": [{"uploaded_at": (at - timedelta(hours=h)).isoformat()} for h in (25, 23)],
    }
    window = managed_window(sample)
    assert window["durable_v2_video_count"] == 1
    assert window["seconds_since_last_upload"] == 23 * 3600
    assert not window["upload_age_at_least_24h"]
    sample["at"] = (at + timedelta(hours=1)).isoformat()
    assert managed_window(sample)["upload_age_at_least_24h"]
    assert not managed_window(sample)["provider_quota_verified"]


def test_network_rates_refuse_restart_counter_reset_and_missing_samples():
    from scripts.phase0_cohort import network_rates

    def point(at, rx, tx, **changes):
        return {
            "transfer": {
                "guest_network": {
                    "status": "observed",
                    "container_id": "guest",
                    "started_at": "boot1",
                    "at": at,
                    "rx_bytes": rx,
                    "tx_bytes": tx,
                    **changes,
                }
            }
        }

    old = point("2026-01-01T00:00:00Z", 100, 200)
    new = point("2026-01-01T00:01:00Z", 700, 1400)
    assert network_rates(old, new)["egress_Bps"] == 20
    assert network_rates(old, new)["ingress_Bps"] == 10
    assert network_rates({}, new)["status"] == "unknown"
    assert network_rates(old, point("2026-01-01T00:01:00Z", 1, 2))["status"] == "unknown"
    assert network_rates(old, point("2026-01-01T00:01:00Z", 700, 1400, started_at="boot2"))["status"] == "unknown"


def test_confirmed_upload_speed_excludes_reused_originals():
    from scripts.phase0_cohort import confirmed_upload_rates

    row = {
        "asset_id": "asset",
        "segment_index": 0,
        "size_bytes": 6000,
        "backup_intent_at": "2026-01-01T00:00:00Z",
        "uploaded_at": "2026-01-01T00:01:00Z",
    }
    rates = confirmed_upload_rates({"managed": {"segments": [row, {**row, "reused": True}]}})
    assert len(rates) == 1 and rates[0]["effective_Bps"] == 100


async def test_scheduling_summary_separates_waiting_from_running():
    from unittest.mock import AsyncMock

    from scripts.phase0_cohort import managed_sample

    pool = AsyncMock()
    pool.fetch.side_effect = [
        [],
        [
            {"stage": "download", "state": "READY", "overdue_check": True, "due_wait_seconds": 12.0},
            {"stage": "download", "state": "READY", "overdue_check": False, "due_wait_seconds": 0.0},
            {"stage": "prepare", "state": "READY", "overdue_check": False, "due_wait_seconds": 4.0},
            {"stage": "upload", "state": "RUNNING", "overdue_check": False, "due_wait_seconds": 100.0},
        ],
        [],
        [],
        [],
        [],
    ]
    report = (await managed_sample(pool, [uuid.uuid4()]))["scheduling_summary"]
    assert report["overdue_download_checks"] == 1
    assert report["waiting_by_stage"] == {
        "download": {"count": 2, "max_due_wait_seconds": 12.0},
        "prepare": {"count": 1, "max_due_wait_seconds": 4.0},
    }


def test_continuous_quota_window_requires_samples_and_resets_on_fifteen_minute_gap():
    from scripts.phase0_cohort import continuous_observation

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    points = [{"at": (start + timedelta(minutes=10 * i)).isoformat()} for i in range(145)]
    assert not continuous_observation([], start.isoformat())["continuous_observation_at_least_24h"]
    assert not continuous_observation(points, None)["continuous_observation_at_least_24h"]
    assert continuous_observation(points, start.isoformat())["continuous_observation_at_least_24h"]
    interrupted = [point for i, point in enumerate(points) if i != 100]
    report = continuous_observation(interrupted, start.isoformat())
    assert not report["continuous_observation_at_least_24h"]
    assert report["continuous_observation_seconds"] == 43 * 600
    assert report["observation_interruptions"][0]["gap_seconds"] == 1200
    assert not continuous_observation(points, (start + timedelta(minutes=1)).isoformat())[
        "continuous_observation_at_least_24h"
    ]


def test_continuous_window_keeps_interruptions_after_a_new_successful_window():
    from scripts.phase0_cohort import continuous_observation

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    points = [{"at": start.isoformat()}]
    points.extend({"at": (start + timedelta(hours=2, minutes=10 * i)).isoformat()} for i in range(145))
    report = continuous_observation(points, start.isoformat())
    assert report["continuous_observation_at_least_24h"]
    assert len(report["observation_interruptions"]) == 1
    with pytest.raises(ValueError, match="distinct"):
        continuous_observation([points[0], points[0]], start.isoformat())
    with pytest.raises(ValueError, match="newer"):
        continuous_observation(points, (start + timedelta(days=2)).isoformat())
