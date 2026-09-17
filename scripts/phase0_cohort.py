#!/usr/bin/env python3
"""Immutable T0 cohort, read-only samples, timing report and operator markers.

Use ``python -m scripts.phase0_cohort --help``. Snapshots never select a fixed
checkpoint count. Sampling is supplemental evidence; exact stage durations
come from PIXAV_PHASE0_EVENTS_DIR spans, not state-polling estimates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pixav.config import get_settings
from pixav.media_loader.qbittorrent import QBitClient
from pixav.shared.db import create_pool
from pixav.shared.phase0_timing import STAGES, failure_class
from pixav.shared.redis_client import create_redis
from scripts.backup_files import create_backup_file
from scripts.instance_guard import database_identity, redis_identity
from scripts.phase0_backlog import _payload_ids, _queue_snapshot


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with create_backup_file(path) as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")


def seconds(value: str) -> float:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("evidence timestamps must include timezone")
    return parsed.timestamp()


def percentiles(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "n": len(values),
        **{f"p{p}": ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)] if ordered else None for p in (50, 95)},
    }


async def sample(pool: Any, redis: Any, settings: Any, task_ids: list[uuid.UUID] | None) -> dict:
    rows = await pool.fetch(
        "SELECT t.id,t.video_id,t.state,t.queue_name,t.retries,t.retry_not_before,t.error_message,"
        "v.info_hash,v.status AS video_status FROM tasks t JOIN videos v ON v.id=t.video_id "
        "WHERE ($1::uuid[] IS NULL AND t.state NOT IN ('complete','failed')) OR t.id=ANY($1::uuid[]) ORDER BY t.id",
        task_ids,
    )
    tasks = [
        {
            "task_id": str(row["id"]),
            "video_id": str(row["video_id"]),
            "state": row["state"],
            "queue_name": row["queue_name"],
            "retries": row["retries"],
            "video_status": row["video_status"],
            "info_hash": row["info_hash"],
            "failure_class": failure_class(row["error_message"] or ""),
            "retry_not_before": row["retry_not_before"].isoformat() if row["retry_not_before"] else None,
        }
        for row in rows
    ]
    queues = {name: await _queue_snapshot(redis, name) for name in (settings.queue_download, settings.queue_upload)}
    processing = [payload for queue in queues.values() for payload in queue["processing"]]
    processing_ids = [task for payload in processing if (task := _payload_ids(payload)[0]) is not None]
    owners = await pool.fetch("SELECT id,state FROM tasks WHERE id=ANY($1::uuid[])", processing_ids)
    open_owners = {str(row["id"]) for row in owners if row["state"] not in {"complete", "failed"}}
    return {
        "schema_version": 1,
        "at": (await pool.fetchval("SELECT clock_timestamp()")).isoformat(),
        "database_identity": await database_identity(pool),
        "database": await pool.fetchval("SELECT current_database()"),
        "redis_run_id": await redis_identity(redis),
        "tasks": tasks,
        "queues": queues,
        "processing_reconciliation": {
            "invalid_payload_count": sum(_payload_ids(payload)[0] is None for payload in processing),
            "unowned_or_terminal_task_ids": sorted({str(task) for task in processing_ids} - open_owners),
        },
    }


async def qbit_inventory(settings: Any, pool: Any) -> dict:
    client = QBitClient(base_url=settings.qbit_url, username=settings.qbit_user, password=settings.qbit_password)
    try:
        hashes = await client.list_torrent_hashes()
        owners = await pool.fetch(
            "SELECT DISTINCT v.info_hash FROM videos v JOIN tasks t ON t.video_id=v.id "
            "WHERE t.state NOT IN ('complete','failed') AND v.info_hash IS NOT NULL"
        )
        owned = {str(row["info_hash"]).lower() for row in owners}
        return {"status": "observed", "hashes": sorted(hashes), "unowned_hashes": sorted(hashes - owned)}
    except Exception:
        return {"status": "unavailable", "hashes": None, "unowned_hashes": None}
    finally:
        await client.aclose()


def build_report(cohort: dict, observations: list[dict], events: list[dict], inventory: dict) -> dict:
    ids = {row["task_id"] for row in cohort["tasks"]}
    start = seconds(cohort["at"])
    ordered = sorted(observations, key=lambda item: seconds(item["at"]))
    if not ordered or any(seconds(item["at"]) < start for item in ordered):
        raise ValueError("report requires post-T0 observations")
    previous_run = cohort["redis_run_id"]
    for item in ordered:
        if any(item[key] != cohort[key] for key in ("database_identity", "database")):
            raise ValueError("cohort instance changed")
        if item["redis_run_id"] != previous_run and item.get("redis_restart_from") != previous_run:
            raise ValueError("cohort instance changed; reconcile and explicitly record the Redis restart")
        previous_run = item["redis_run_id"]
    latest = {row["task_id"]: row for row in ordered[-1]["tasks"]}
    unfinished = sorted(task for task in ids if not _terminal(latest.get(task, {})))
    accepted = [
        e for e in events if e.get("task_id") in ids and start <= seconds(e["at"]) <= seconds(ordered[-1]["at"])
    ]
    starts = {e["span_id"]: e for e in accepted if e.get("event") == "start"}
    ends = {e["span_id"]: e for e in accepted if e.get("event") == "end"}
    durations = {stage: [] for stage in STAGES}
    for span, event in ends.items():
        if span in starts and event.get("outcome") in {"complete", "failed"} and event.get("stage") in durations:
            durations[event["stage"]].append(event["elapsed_seconds"])
    # Queue and retry residence estimates explicitly preserve polling uncertainty.
    residence: dict[str, dict[str, float]] = {"queue_wait_sampled": {}, "retry_wait_sampled": {}}
    for before, after in zip([cohort, *ordered[:-1]], ordered, strict=True):
        delta = seconds(after["at"]) - seconds(before["at"])
        for row in before["tasks"]:
            task = row["task_id"]
            if task in ids and row["state"] in {"pending", "dispatched"}:
                label = "retry_wait_sampled" if row["retry_not_before"] else "queue_wait_sampled"
                residence[label][task] = residence[label].get(task, 0) + delta
    processing = [payload for queue in ordered[-1]["queues"].values() for payload in queue["processing"]]
    processing_orphans = [p for p in processing if p.get("task_id") in ids and p.get("task_id") not in unfinished]
    return {
        "schema_version": 1,
        "cohort_size": len(ids),
        "t0": cohort["at"],
        "observed_until": ordered[-1]["at"],
        "wall_clock_seconds": seconds(ordered[-1]["at"]) - start,
        "classification_complete": bool(ids) and not unfinished,
        "first_complete_observation_at": next(
            (
                item["at"]
                for item in ordered
                if ids and all(_terminal({row["task_id"]: row for row in item["tasks"]}.get(task, {})) for task in ids)
            ),
            None,
        ),
        "unfinished_task_ids": unfinished,
        "stage_seconds": {stage: percentiles(values) for stage, values in durations.items()},
        "sampled_residence_seconds": {name: percentiles(list(values.values())) for name, values in residence.items()},
        "max_sampling_interval_seconds": max(
            seconds(b["at"]) - seconds(a["at"]) for a, b in zip([cohort, *ordered[:-1]], ordered, strict=True)
        ),
        "open_span_ids": sorted(starts.keys() - ends.keys()),
        "unmatched_end_span_ids": sorted(ends.keys() - starts.keys()),
        "attempt_outcomes": [
            {key: event.get(key) for key in ("task_id", "stage", "outcome", "failure_class", "elapsed_seconds")}
            for event in sorted(ends.values(), key=lambda event: seconds(event["at"]))
        ],
        "missing_stage_evidence": [stage for stage, values in durations.items() if not values],
        "retry_counters": {task: latest.get(task, {}).get("retries") for task in sorted(ids)},
        "failure_classes": {task: latest.get(task, {}).get("failure_class") for task in sorted(ids)},
        "operator_markers": [
            e
            for e in events
            if e.get("event") in {"operator_pause", "operator_resume", "operator_interruption"}
            and start <= seconds(e["at"]) <= seconds(ordered[-1]["at"])
        ],
        "processing": processing,
        "cohort_processing_orphans": processing_orphans,
        "processing_reconciliation": ordered[-1].get("processing_reconciliation"),
        "redis_restarts": [
            {"at": item["at"], "from": item["redis_restart_from"], "to": item["redis_run_id"]}
            for item in ordered
            if item.get("redis_restart_from")
        ],
        "qbit": inventory,
        "production_gate": "OPEN",  # Timing alone cannot prove playback/VPN/promotion.
    }


def _terminal(row: dict) -> bool:
    return row.get("state") == "failed" or (row.get("state") == "complete" and row.get("video_status") == "available")


async def run(args: argparse.Namespace) -> None:
    root = args.directory
    manifest = root / "cohort.json"
    if args.command == "mark":
        if not manifest.is_file():
            raise RuntimeError("snapshot the cohort first")
        write_json(
            root / "events" / f"{uuid.uuid4()}.json",
            {
                "event": args.event,
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return
    cohort = json.loads(manifest.read_text()) if args.command != "snapshot" else None
    settings = get_settings()
    pool = await create_pool(settings)
    redis = await create_redis(settings)
    try:
        if (
            await database_identity(pool) != args.expect_db_identity
            or await redis_identity(redis) != args.expect_redis_identity
        ):
            raise RuntimeError("cohort requires fresh exact PostgreSQL and Redis identities")
        observation = await sample(
            pool, redis, settings, [uuid.UUID(row["task_id"]) for row in cohort["tasks"]] if cohort else None
        )
        if cohort:
            if any(observation[key] != cohort[key] for key in ("database_identity", "database")):
                raise RuntimeError("cohort instance changed")
            previous = sorted(
                [cohort, *(json.loads(p.read_text()) for p in (root / "samples").glob("*.json"))],
                key=lambda item: seconds(item["at"]),
            )[-1]
            if observation["redis_run_id"] != previous["redis_run_id"]:
                if not args.accept_redis_restart:
                    raise RuntimeError("reconcile Redis restart and pass --accept-redis-restart with fresh identity")
                observation["redis_restart_from"] = previous["redis_run_id"]
            write_json(root / "samples" / f"{uuid.uuid4()}.json", observation)
        else:
            write_json(manifest, observation)
        if args.command == "report":
            observations = [json.loads(p.read_text()) for p in (root / "samples").glob("*.json")]
            events = [json.loads(p.read_text()) for p in (root / "events").glob("*.json")]
            report = build_report(cohort, observations, events, await qbit_inventory(settings, pool))
            write_json(root / "reports" / f"{uuid.uuid4()}.json", report)
            print(json.dumps(report, indent=2))
    finally:
        await redis.aclose()
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot", "sample", "report", "mark"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--expect-db-identity", default="")
    parser.add_argument("--expect-redis-identity", default="")
    parser.add_argument("--accept-redis-restart", action="store_true")
    parser.add_argument(
        "--event",
        choices=("operator_pause", "operator_resume", "operator_interruption"),
        default="operator_interruption",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"cohort operation refused: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
