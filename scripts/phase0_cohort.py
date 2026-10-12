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
import re
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
    # Docker timestamps carry nanoseconds and Z; Python 3.10 accepts microseconds.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
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


async def managed_sample(pool: Any, task_ids: list[uuid.UUID]) -> dict:
    """Read explicit columns only: never persist checkpoint, credentials or URLs.

    The caller holds a read-only repeatable-read transaction so asset, segment
    and quota facts cannot straddle a successful upload commit.
    """
    queries = {
        "executions": "SELECT id,task_id,state,stage,generation,owner,lease_until,due_at,"
        "failure_class,created_at,updated_at FROM executions WHERE task_id=ANY($1::uuid[]) ORDER BY id",
        "scheduling": "SELECT e.id,e.stage,e.state,"
        "(e.stage='download' AND e.state IN ('READY','WAITING_RETRY') AND e.due_at<now()) AS overdue_check,"
        "GREATEST(0,extract(epoch FROM now()-e.due_at))::double precision AS due_wait_seconds,"
        "e.checkpoint#>>'{download_poll,last_checked_at}' AS download_last_checked_at,"
        "e.checkpoint->>'download_confirmed_at' AS download_confirmed_at,"
        "extract(epoch FROM (SELECT min(a.started_at) FROM activity_attempts a "
        "WHERE a.execution_id=e.id AND a.stage='prepare'))::double precision "
        "- (e.checkpoint->>'torrent_completed_at')::double precision AS completion_to_prepare_seconds "
        "FROM executions e WHERE e.task_id=ANY($1::uuid[]) ORDER BY e.id",
        "attempts": "SELECT a.id,a.execution_id,a.generation,a.stage,a.operation_id,a.started_at,a.completed_at "
        "FROM activity_attempts a JOIN executions e ON e.id=a.execution_id "
        "WHERE e.task_id=ANY($1::uuid[]) ORDER BY a.started_at,a.id",
        "assets": "SELECT r.id,r.video_id,r.state,r.policy_version,r.segment_count,r.durable_at "
        "FROM remote_assets r JOIN workflow_tasks w ON w.video_id=r.video_id "
        "WHERE w.task_id=ANY($1::uuid[]) ORDER BY r.id",
        "segments": "SELECT DISTINCT s.asset_id,s.segment_index,s.state,s.size_bytes,s.sha256,"
        "s.account_id,s.uploaded_at,s.usage_counted_at,s.retry_not_before "
        ",s.recovery#>>'{operations,backup,intent_at}' AS backup_intent_at,"
        "s.verification ? 'reconciled_from' AS reused "
        "FROM remote_asset_segments s JOIN remote_assets r ON r.id=s.asset_id "
        "JOIN workflow_tasks w ON w.video_id=r.video_id "
        "WHERE w.task_id=ANY($1::uuid[]) ORDER BY s.asset_id,s.segment_index",
        "quota": "SELECT id,status,daily_uploaded_bytes,daily_quota_bytes,quota_reset_at,cooldown_until "
        "FROM accounts WHERE id IN (SELECT s.account_id FROM remote_asset_segments s "
        "JOIN remote_assets r ON r.id=s.asset_id JOIN workflow_tasks w ON w.video_id=r.video_id "
        "WHERE w.task_id=ANY($1::uuid[])) ORDER BY id",
    }
    result = {}
    for name, query in queries.items():
        result[name] = [
            {
                key: (
                    value.isoformat()
                    if isinstance(value, datetime)
                    else str(value) if isinstance(value, uuid.UUID) else value
                )
                for key, value in dict(row).items()
            }
            for row in await pool.fetch(query, task_ids)
        ]
    waiting: dict[str, dict[str, float | int]] = {}
    for row in result["scheduling"]:
        if row["state"] not in {"READY", "WAITING_RETRY", "WAITING_QUOTA"}:
            continue
        stage = waiting.setdefault(row["stage"], {"count": 0, "max_due_wait_seconds": 0.0})
        stage["count"] += 1
        stage["max_due_wait_seconds"] = max(stage["max_due_wait_seconds"], row["due_wait_seconds"])
    result["scheduling_summary"] = {
        "overdue_download_checks": sum(bool(row["overdue_check"]) for row in result["scheduling"]),
        "waiting_by_stage": waiting,
    }
    return result


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
        "schema_version": 2,
        "managed": await managed_sample(pool, [row["id"] for row in rows]) if settings.managed_media_workflow else None,
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


def guest_network(owner: str) -> dict:
    """One owned namespace only; tools shares it and must not be counted twice."""
    import docker

    client = docker.from_env(timeout=10)
    try:
        guests = client.containers.list(
            filters={
                "label": [
                    f"pixav.photos_canary.owner={owner}",
                    "pixav.photos_canary.role=guest",
                ]
            }
        )
        if len(guests) != 1:
            return {"status": "unavailable", "guest_count": len(guests)}
        guest = guests[0]
        stats = guest.stats(stream=False)
        networks = stats.get("networks", {})
        return {
            "status": "observed",
            "container_id": guest.id,
            "started_at": guest.attrs["State"]["StartedAt"],
            "at": stats["read"],
            "rx_bytes": sum(n["rx_bytes"] for n in networks.values()),
            "tx_bytes": sum(n["tx_bytes"] for n in networks.values()),
            "scope": "guest network including background traffic; not confirmed Photos upload bytes",
        }

    finally:
        client.close()


async def transfer_sample(settings: Any) -> dict:
    client = QBitClient(settings.qbit_url, settings.qbit_user, settings.qbit_password, timeout=10)
    result: dict = {}
    try:
        result["qbit"] = await client.transfer_snapshot()
    except Exception as error:
        result["qbit"] = {"status": "unavailable", "error_type": type(error).__name__}
    finally:
        await client.aclose()
    try:
        result["guest_network"] = await asyncio.to_thread(guest_network, str(settings.storage_worker_owner))
    except Exception as error:
        result["guest_network"] = {"status": "unavailable", "error_type": type(error).__name__}
    return result


def network_rates(previous: dict, current: dict) -> dict:
    """Reset/restarted/missing counters are unknown, never a zero-speed claim."""
    old = previous.get("transfer", {}).get("guest_network", {})
    new = current.get("transfer", {}).get("guest_network", {})
    if (
        old.get("status") != "observed"
        or new.get("status") != "observed"
        or old.get("container_id") != new.get("container_id")
        or old.get("started_at") != new.get("started_at")
    ):
        return {"status": "unknown"}
    elapsed = seconds(new["at"]) - seconds(old["at"])
    if elapsed <= 0 or any(new[k] < old[k] for k in ("rx_bytes", "tx_bytes")):
        return {"status": "unknown"}
    return {
        "status": "observed",
        "interval_seconds": elapsed,
        "ingress_Bps": (new["rx_bytes"] - old["rx_bytes"]) / elapsed,
        "egress_Bps": (new["tx_bytes"] - old["tx_bytes"]) / elapsed,
        "scope": "guest network; not Photos-specific",
    }


def confirmed_upload_rates(observation: dict) -> list[dict]:
    result = []
    for row in (observation.get("managed") or {}).get("segments", []):
        if row.get("reused") or not row.get("uploaded_at") or not row.get("backup_intent_at"):
            continue
        elapsed = seconds(row["uploaded_at"]) - seconds(row["backup_intent_at"])
        if elapsed > 0:
            result.append(
                {
                    "asset_id": row["asset_id"],
                    "segment_index": row["segment_index"],
                    "bytes": row["size_bytes"],
                    "seconds": elapsed,
                    "effective_Bps": row["size_bytes"] / elapsed,
                    "scope": "backup intent to confirmed result; not wire speed",
                }
            )
    return result


def continuous_observation(samples: list[dict], last_upload: str | None) -> dict:
    """Preserve interruptions; elapsed upload age cannot establish continuity."""
    ordered = sorted(samples, key=lambda item: seconds(item["at"]))
    interruptions = []
    beginning = seconds(ordered[0]["at"]) if ordered else None
    for before, after in zip(ordered, ordered[1:], strict=False):
        gap = seconds(after["at"]) - seconds(before["at"])
        if gap <= 0:
            raise ValueError("observation timestamps must be distinct")
        if gap > 900:
            interruptions.append({"before": before["at"], "after": after["at"], "gap_seconds": gap})
            beginning = seconds(after["at"])
    elapsed = 0.0
    if last_upload and beginning is not None and len(ordered) >= 2:
        end = seconds(ordered[-1]["at"])
        uploaded = seconds(last_upload)
        if uploaded > end:
            raise ValueError("upload timestamp is newer than the observation")
        elapsed = max(0.0, end - max(beginning, uploaded))
    return {
        "continuous_observation_seconds": elapsed,
        "continuous_observation_at_least_24h": elapsed >= 86400,
        "observation_interruptions": interruptions,
        "maximum_allowed_interval_seconds": 900,
    }


def managed_window(observation: dict, *, samples: list[dict] | None = None) -> dict:
    """Summarize DB facts without treating them as live acceptance evidence."""
    managed = observation.get("managed") or {}
    assets = managed.get("assets", [])
    uploads = [row["uploaded_at"] for row in managed.get("segments", []) if row.get("uploaded_at")]
    latest = max(uploads, key=seconds) if uploads else None
    elapsed = seconds(observation["at"]) - seconds(latest) if latest else None
    return {
        "durable_v2_video_count": len(
            {
                row["video_id"]
                for row in assets
                if row["state"] == "DURABLE" and row["policy_version"] == "photos-original-v2"
            }
        ),
        "segmented_asset_count": sum(row.get("segment_count", 0) > 1 for row in assets),
        "last_uploaded_at": latest,
        "seconds_since_last_upload": elapsed,
        "upload_age_at_least_24h": elapsed is not None and elapsed >= 86400,
        "provider_quota_verified": False,
        "restart_recovery_verified": False,
        **continuous_observation(samples or [], latest),
    }


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
        "managed": ordered[-1].get("managed"),
        "transfer": ordered[-1].get("transfer"),
        "managed_window": managed_window(
            ordered[-1], samples=[cohort, *ordered] if seconds(cohort["at"]) < seconds(ordered[0]["at"]) else ordered
        ),
        "photos_acceptance": "OPEN",  # Counts cannot establish cold-path or provider quota evidence.
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
        selected = [uuid.UUID(row["task_id"]) for row in cohort["tasks"]] if cohort else args.task_id
        async with pool.acquire() as connection:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                observation = await sample(connection, redis, settings, selected)
        observation["transfer"] = await transfer_sample(settings)
        observation["transfer"]["confirmed_uploads"] = confirmed_upload_rates(observation)
        observation["transfer"]["storage_executions"] = [
            {k: row[k] for k in ("id", "state", "stage")}
            for row in (observation.get("managed") or {}).get("executions", [])
            if row["stage"] in {"upload", "verify"}
        ]
        if selected is not None and {str(item) for item in selected} != {
            row["task_id"] for row in observation["tasks"]
        }:
            raise RuntimeError("selected cohort tasks are missing")
        if cohort:
            if any(observation[key] != cohort[key] for key in ("database_identity", "database")):
                raise RuntimeError("cohort instance changed")
            previous = sorted(
                [cohort, *(json.loads(p.read_text()) for p in (root / "samples").glob("*.json"))],
                key=lambda item: seconds(item["at"]),
            )[-1]
            observation["transfer"]["guest_rates"] = network_rates(previous, observation)
            if observation["redis_run_id"] != previous["redis_run_id"]:
                if not args.accept_redis_restart:
                    raise RuntimeError("reconcile Redis restart and pass --accept-redis-restart with fresh identity")
                observation["redis_restart_from"] = previous["redis_run_id"]
            write_json(root / "samples" / f"{uuid.uuid4()}.json", observation)
        else:
            write_json(manifest, observation)
        latest = root / f"transfer-{uuid.uuid4()}.tmp"
        write_json(latest, {"at": observation["at"], **observation["transfer"]})
        latest.replace(root / "transfer-latest.json")
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
    parser.add_argument("--task-id", type=uuid.UUID, action="append", help="snapshot exact task; repeatable")
    parser.add_argument("--expect-db-identity", default="")
    parser.add_argument("--expect-redis-identity", default="")
    parser.add_argument("--accept-redis-restart", action="store_true")
    parser.add_argument(
        "--event",
        choices=("operator_pause", "operator_resume", "operator_interruption"),
        default="operator_interruption",
    )
    args = parser.parse_args()
    if args.task_id and args.command != "snapshot":
        parser.error("--task-id is only valid for snapshot; subsequent samples use the frozen cohort")
    try:
        asyncio.run(run(args))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"cohort operation refused: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
