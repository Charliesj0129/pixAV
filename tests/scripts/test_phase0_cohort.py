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
