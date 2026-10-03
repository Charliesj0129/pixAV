#!/usr/bin/env python3
"""Inspect and steer the library projection.

``status`` shows publication states, and for one video its overrides and
retained artwork. ``override set|clear`` records a person's value for one
metadata field; no scraper can overwrite it, and clearing returns the field to
the providers. ``artwork import`` retains one JPEG, PNG or WebP original under
its SHA-256; projections only ever use retained originals. ``reconcile`` shows
what a pass would change, and with ``--apply`` performs it.

Every write names an operator and a reason, echoes the database identity first,
and refuses a different cluster when ``--db-identity`` is given. Nothing here
prints a credential, a share location or a local media path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from pixav.config import get_settings
from pixav.library_projection.artwork import ArtworkStore
from pixav.library_projection.metadata import OVERRIDE_FIELDS
from pixav.library_projection.reconciler import build_reconciler
from pixav.library_projection.repository import LibraryRepository
from pixav.shared.db import create_pool
from pixav.shared.instance import database_identity

_STATUS_SQL = """
SELECT video_id, state, playable_asset_id, projection_version, active_version, failure_code,
       published_at, withdrawn_at, withdrawn_reason, updated_at
  FROM library_publications
 WHERE $1::uuid IS NULL OR video_id = $1
 ORDER BY updated_at DESC
 LIMIT $2
"""


def _render(row: Any) -> dict:
    return {key: (None if value is None else str(value)) for key, value in dict(row).items()}


def _override_value(field: str, raw: str) -> object:
    """List fields take a JSON array; the others take the text as given."""
    if field in ("performers", "tags"):
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ValueError(f'{field} takes a JSON array, for example \'["a", "b"]\'') from exc
    return raw


async def _status(args: argparse.Namespace, pool: Any, identity: str) -> int:
    rows = await pool.fetch(_STATUS_SQL, args.video_id, args.limit)
    report: dict[str, Any] = {"database_identity": identity, "publications": [_render(row) for row in rows]}
    if args.video_id is not None:
        repository = LibraryRepository(pool)
        report["overrides"] = await repository.overrides(args.video_id)
        report["artwork"] = [
            {"kind": record.kind, "sha256": record.sha256, "media_type": record.media_type}
            for record in await repository.retained_artwork(args.video_id)
        ]
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


async def _write(args: argparse.Namespace, pool: Any) -> int:
    settings = get_settings()
    repository = LibraryRepository(pool)
    if args.command == "override" and args.action == "set":
        stored = await repository.set_override(
            args.video_id,
            args.field,
            _override_value(args.field, args.value),
            operator=args.operator,
            reason=args.reason,
        )
        print(f"{args.field} of {args.video_id} is now {json.dumps(stored, ensure_ascii=False)}")
        return 0
    if args.command == "override":
        cleared = await repository.clear_override(args.video_id, args.field, operator=args.operator, reason=args.reason)
        print(f"{args.field} of {args.video_id} " + ("returned to the providers" if cleared else "had no override"))
        return 0 if cleared else 2
    if args.command == "artwork":
        digest, size, media_type = await asyncio.to_thread(
            ArtworkStore(Path(settings.artwork_root)).import_file, args.path
        )
        await repository.record_artwork(
            args.video_id,
            args.kind,
            sha256=digest,
            size_bytes=size,
            media_type=media_type,
            provenance={"source": "operator-import", "operator": args.operator, "reason": args.reason},
        )
        print(f"retained {args.kind} {digest} ({media_type}, {size} bytes) for {args.video_id}")
        return 0
    reconciler = build_reconciler(pool, settings)
    if not args.apply:
        for decision in await reconciler.plan():
            if decision.action != "unchanged":
                print(f"{decision.video_id}: {decision.action}" + (f" ({decision.code})" if decision.code else ""))
        print("dry run: nothing was written; re-run with --apply")
        return 0
    print(json.dumps(await reconciler.reconcile(), sort_keys=True))
    return 0


async def _run(args: argparse.Namespace) -> int:
    pool = await create_pool(get_settings())
    try:
        identity = await database_identity(pool)
        if args.db_identity and args.db_identity != identity:
            print(f"refusing: database identity is {identity}, expected {args.db_identity}")
            return 2
        if args.command == "status":
            return await _status(args, pool, identity)
        print(f"database identity: {identity}")
        try:
            return await _write(args, pool)
        except (ValueError, OSError) as exc:
            print(f"refused: {exc}")
            return 2
    finally:
        await pool.close()


def _audited(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--operator", required=True, help="who is making this change")
    parser.add_argument("--reason", required=True, help="why, recorded with the change")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-identity", default=None, help="refuse to act on a different PostgreSQL cluster")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="publication states; overrides and artwork for one video")
    status.add_argument("--video-id", type=uuid.UUID, default=None)
    status.add_argument("--limit", type=int, default=50)

    override = commands.add_parser("override", help="set or clear a manual metadata value")
    actions = override.add_subparsers(dest="action", required=True)
    setting = actions.add_parser("set")
    setting.add_argument("video_id", type=uuid.UUID)
    setting.add_argument("field", choices=OVERRIDE_FIELDS)
    setting.add_argument("value", help="text, an ISO date for release_date, or a JSON array for performers/tags")
    _audited(setting)
    clearing = actions.add_parser("clear")
    clearing.add_argument("video_id", type=uuid.UUID)
    clearing.add_argument("field", choices=OVERRIDE_FIELDS)
    _audited(clearing)

    artwork = commands.add_parser("artwork", help="retain an artwork original")
    artwork_actions = artwork.add_subparsers(dest="action", required=True)
    importing = artwork_actions.add_parser("import")
    importing.add_argument("video_id", type=uuid.UUID)
    importing.add_argument("kind", choices=("poster", "backdrop"))
    importing.add_argument("path", type=Path)
    _audited(importing)

    reconcile = commands.add_parser("reconcile", help="show, or with --apply perform, one reconciliation pass")
    reconcile.add_argument("--apply", action="store_true")

    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
