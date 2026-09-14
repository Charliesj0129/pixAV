"""Exact-state capture at the boundaries where the supervisor hands control over.

Before the supervisor re-enters the CLI at a stage boundary it takes a full
`pg_dump` plus a selected-row snapshot of precisely the rows this run owns, and
proves the instance is still the one it was authorized against. The selected
rows exist because a full dump alone cannot show, months later, which rows the
boundary actually concerned; the identity checks exist because this host also
runs `pixav-postgres` and `pixav-integration-postgres-1`, and a backup taken
against the wrong instance is worse than none.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import redis.asyncio as aioredis
from pydantic import BaseModel

from pixav.pixel_injector.canary import single_flight
from pixav.shared.video_parts import VideoPartRepository

from .evidence import command, event, stamp, verify_sources, write
from .guards import preflight
from .recovery import retained, snapshot
from .settings import PROJECT, REDIS, WORK, container


class Expectation(BaseModel):
    """The instance the operator authorized this supervision against.

    Loaded from a private 0600 file rather than written into the tree: these
    are host fingerprints, they change whenever the machine reboots into a new
    Redis, and pinning them in git would make every checkout claim an identity
    it does not have.
    """

    model_config = {"frozen": True}

    docker_id: str
    database: str
    db_identity: str
    redis_identity: str
    windows_vlc_sha256: str | None = None
    windows_vlc_path: str = "pixav-acceptance\\vlc-3.0.23\\vlc.exe"


class Supervision(BaseModel):
    """One supervision: which run, which instance, and where evidence goes."""

    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    run_id: uuid.UUID
    expectation: Expectation
    out: Path
    digests: dict[str, str]
    authorization: str
    """Why a local 0600 backup of this run was permitted, recorded beside every dump."""


async def redis_snapshot(expected_identity: str) -> list[dict]:
    """Dump every Redis key, refusing a snapshot taken across an instance change."""
    redis = aioredis.from_url(REDIS)
    try:
        if (await redis.info("server"))["run_id"] != expected_identity:
            raise RuntimeError("Redis changed before backup")
        values = []
        async for key in redis.scan_iter():
            data = await redis.dump(key)
            if data is None:
                raise RuntimeError("Redis changed during backup")
            values.append(
                {
                    "key_base64": base64.b64encode(key).decode(),
                    "dump_base64": base64.b64encode(data).decode(),
                    "pttl": await redis.pttl(key),
                }
            )
        return sorted(values, key=lambda value: value["key_base64"])
    finally:
        await redis.aclose()


def _check_drill_boundary(state: dict, parts: list) -> None:
    """The recovery drill is only meaningful at exactly one point in the run.

    It proves a resume across a confirmed-part boundary republishes nothing, so
    it needs exactly one confirmed part, at least one still to come, and no
    unconfirmed part carrying recovery state from an interrupted attempt.
    """
    confirmed = [p for p in parts if p.usage_counted_at is not None]
    if (
        state["stage"] != "upload_paused"
        or len(parts) < 2
        or len(confirmed) != 1
        or confirmed[0].part_index != 0
        or state.get("recovery_drill")
        or any(p.state != "prepared" or p.recovery for p in parts if p.usage_counted_at is None)
    ):
        raise RuntimeError("not the exact first-confirmed-part boundary")
    if not confirmed[0].share_url or not confirmed[0].recovery.get("media_id"):
        raise RuntimeError("confirmed part lacks backup/share identity")


async def _runtime_for(label: str, client: Any, state: dict, run_id: uuid.UUID, parts: list) -> tuple[Any, Any]:
    if label == "before-preparation":
        if state["stage"] != "preparing_media" or parts or state.get("runtime"):
            raise RuntimeError("preparation reconciliation target changed")
        return None, None
    flow = SimpleNamespace(client=client, state=state, id=run_id)
    guest, runner = await retained(flow, WORK)
    if any(c.status != "running" for c in (guest, runner)):
        raise RuntimeError("retained guest/tools not running")
    return guest, runner


async def backup_boundary(client: Any, conn: Any, state: dict, label: str, supervision: Supervision) -> None:
    """Capture the full and selected state of this run, then record that it happened."""
    expectation, out = supervision.expectation, supervision.out
    # Caller owns the DB advisory lock. Also exclude every local canary runner.
    with single_flight():
        verify_sources(supervision.digests)
        # preflight reads nothing off its namespace; an empty one is the whole point.
        evidence = await preflight(client, argparse.Namespace())
        if any(evidence[key] != getattr(expectation, key) for key in ("db_identity", "redis_identity")):
            raise RuntimeError("preflight identity changed")
        video = uuid.UUID(state["video_id"])
        parts = await VideoPartRepository(conn).list(video)
        if label == "before-drill":
            _check_drill_boundary(state, parts)
        guest, runner = await _runtime_for(label, client, state, supervision.run_id, parts)
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            exported = await conn.fetchval("SELECT pg_export_snapshot()")
            rows = {}
            for table, sql, args in [
                ("run", "SELECT to_jsonb(r) FROM first_4k_runs r WHERE id=$1", [supervision.run_id]),
                ("video", "SELECT to_jsonb(v) FROM videos v WHERE id=$1", [video]),
                ("parts", "SELECT to_jsonb(p) FROM video_parts p WHERE video_id=$1 ORDER BY part_index", [video]),
                (
                    "accounts",
                    "SELECT to_jsonb(a) FROM accounts a "
                    "WHERE id IN (SELECT account_id FROM video_parts WHERE video_id=$1)",
                    [video],
                ),
                ("tasks", "SELECT to_jsonb(t) FROM tasks t WHERE video_id=$1", [video]),
            ]:
                rows[table] = [json.loads(r[0]) for r in await conn.fetch(sql, *args)]
            if len(rows["run"]) != 1 or len(rows["video"]) != 1 or len(rows["parts"]) != len(parts):
                raise RuntimeError("exact backup target/count changed")
            container(client, "postgres")
            dump = await asyncio.to_thread(
                command,
                [
                    "docker",
                    "exec",
                    f"{PROJECT}-postgres-1",
                    "pg_dump",
                    "-U",
                    "pixav_test",
                    "-d",
                    expectation.database,
                    "-Fc",
                    "--snapshot",
                    exported,
                ],
            )
            toc = await asyncio.to_thread(
                command, ["docker", "exec", "-i", f"{PROJECT}-postgres-1", "pg_restore", "--list"], data=dump
            )
            if not all(name.encode() in toc for name in ("video_parts", "first_4k_runs", "accounts")):
                raise RuntimeError("full backup archive missing required tables")
            # Twice, because a value that moved between them would make the
            # snapshot a mixture of two states rather than a restorable one.
            before = await redis_snapshot(expectation.redis_identity)
            after = await redis_snapshot(expectation.redis_identity)
            if [(v["key_base64"], v["dump_base64"]) for v in before] != [
                (v["key_base64"], v["dump_base64"]) for v in after
            ]:
                raise RuntimeError("Redis values changed during selected backup")
            meta = {
                **expectation.model_dump(mode="json"),
                "system_identifier": expectation.db_identity,
                "local_backup_exception": supervision.authorization,
                "at": stamp(),
                "run_id": str(supervision.run_id),
                "video_id": str(video),
                "sha256": hashlib.sha256(dump).hexdigest(),
                "counts": {k: len(v) for k, v in rows.items()},
                "confirmed_snapshot": snapshot(parts),
                "redis_keys": len(before),
                "guest_id": guest.id if guest else None,
                "tools_id": runner.id if runner else None,
                "pg_restore_list": "PASS",
            }
            write(out, label + ".dump", dump)
            write(out, label + ".dump.meta.json", meta)
            write(out, label + ".selected.json", {"identity": meta, "rows": rows, "redis": before})
        event(
            out,
            "BACKUP_COMPLETE",
            label=label,
            parts=len(parts),
            confirmed=sum(p.usage_counted_at is not None for p in parts),
        )
