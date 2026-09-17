#!/usr/bin/env python3
"""Admit an operator-supplied file into the managed pipeline and watch it.

The download stage only accepts a swarm identified by a 40-hex info_hash, so a
canary file that already exists locally has no ordinary way in. ``admit`` uses
``MediaWorkflow.adopt_local_source``, which records the file as
``operator-supplied`` with the reference it came from: nothing downstream can
mistake it for something the pipeline fetched by itself.

Nothing here prints a credential, a share location or a local path. ``status``
reports states, identifiers, byte counts and hashes — the facts an operator
needs to tell a real upload from a fallback — and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from pixav.config import get_settings
from pixav.maxwell_core.media_workflow import MediaWorkflow
from pixav.shared.db import create_pool
from pixav.shared.instance import database_identity

# Only these reach stdout. share_url and local paths are deliberately absent.
EXECUTION_FIELDS = ("id", "state", "stage", "generation", "blocked_reason", "failure_class", "error_code")
SEGMENT_FIELDS = ("segment_index", "state", "size_bytes", "sha256", "usage_counted_at", "uploaded_at")


def _source(path: str) -> Path:
    """Keep the path exactly as the operator wrote it.

    ``local_path`` is shared between the workers, which see this checkout at
    different absolute locations, so the convention is a workdir-relative path
    like ``./data/canary/file.mp4``. Resolving it here would record one
    container's view and make the file unopenable in the others.
    """
    artifact = Path(path)
    if artifact.is_symlink() or not artifact.is_file():
        raise SystemExit(f"refusing: source is not a regular file: {path}")
    return artifact


async def _admit(conn_pool: Any, args: argparse.Namespace) -> int:
    workflow = MediaWorkflow(conn_pool)
    artifact = _source(args.path)
    async with conn_pool.acquire() as conn:
        video_id = await conn.fetchval(
            "INSERT INTO videos(id,title,status) VALUES($1,$2,'discovered') RETURNING id",
            uuid.uuid4(),
            args.title,
        )
    task_id = await workflow.admit(video_id)
    async with conn_pool.acquire() as conn:
        execution_id = await conn.fetchval(
            "SELECT id FROM executions WHERE task_id=$1 ORDER BY created_at DESC LIMIT 1", task_id
        )
    try:
        operation_id = await workflow.adopt_local_source(
            execution_id,
            path=str(artifact),
            declared_sha256=args.sha256,
            source_url=args.source_url,
            operator=args.operator,
            reason=args.reason,
        )
    except ValueError as exc:
        print(f"refused: {exc}")
        return 2
    print(
        json.dumps(
            {
                "video_id": str(video_id),
                "task_id": str(task_id),
                "execution_id": str(execution_id),
                "download_operation": str(operation_id),
                "provenance": "operator-supplied",
            },
            indent=2,
        )
    )
    return 0


async def _status(conn_pool: Any, args: argparse.Namespace) -> int:
    async with conn_pool.acquire() as conn:
        execution = await conn.fetchrow("SELECT * FROM executions WHERE id=$1", args.execution_id)
        if execution is None:
            print(f"execution not found: {args.execution_id}")
            return 2
        checkpoint = json.loads(execution["checkpoint"])
        asset_id = checkpoint.get("asset_id")
        report: dict = {key: str(execution[key]) if execution[key] is not None else None for key in EXECUTION_FIELDS}
        report["asset_id"] = asset_id
        if asset_id:
            asset = await conn.fetchrow("SELECT * FROM remote_assets WHERE id=$1", uuid.UUID(asset_id))
            report["asset_state"] = asset["state"] if asset else None
            report["asset_durable_at"] = str(asset["durable_at"]) if asset and asset["durable_at"] else None
            segments = await conn.fetch(
                "SELECT * FROM remote_asset_segments WHERE asset_id=$1 ORDER BY segment_index", uuid.UUID(asset_id)
            )
            report["segments"] = [
                {
                    **{key: (str(row[key]) if row[key] is not None else None) for key in SEGMENT_FIELDS},
                    # Presence, never the location itself.
                    "has_share_url": row["share_url"] is not None,
                    "readback_method": json.loads(row["verification"]).get("readback", {}).get("method"),
                    "readback_cold_inputs": json.loads(row["verification"]).get("readback", {}).get("cold_inputs"),
                }
                for row in segments
            ]
    print(json.dumps(report, indent=2))
    return 0


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    pool = await create_pool(settings)
    try:
        identity = await database_identity(pool)
        print(f"database identity: {identity}")
        if args.db_identity and args.db_identity != identity:
            print(f"refusing: expected database identity {args.db_identity}")
            return 2
        if args.command == "admit":
            return await _admit(pool, args)
        return await _status(pool, args)
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-identity", default=None, help="refuse to act on a different PostgreSQL cluster")
    commands = parser.add_subparsers(dest="command", required=True)

    admit = commands.add_parser("admit", help="create a managed execution for an operator-supplied file")
    admit.add_argument("--path", required=True, help="the file to upload")
    admit.add_argument("--sha256", required=True, help="expected digest; the file is re-hashed and must match")
    admit.add_argument("--title", required=True, help="video title recorded in the catalogue")
    admit.add_argument("--source-url", required=True, help="where the operator obtained the file")
    admit.add_argument("--operator", required=True, help="who is taking responsibility for this")
    admit.add_argument("--reason", required=True, help="why, recorded with the artifact provenance")

    status = commands.add_parser("status", help="show the execution, its asset and its segments")
    status.add_argument("execution_id", type=uuid.UUID)

    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
