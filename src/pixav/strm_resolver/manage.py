"""Identity-checked, dry-run-by-default preparation/publication operator entry."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID

import asyncpg

from pixav.config import get_settings
from pixav.shared.instance import database_identity
from pixav.strm_resolver.playback import PlaybackService
from pixav.strm_resolver.projection import LibraryProjection


async def run(args) -> dict:
    settings = get_settings()
    pool = await asyncpg.create_pool(settings.dsn, min_size=1, max_size=3)
    try:
        identity = await database_identity(pool)
        database = await pool.fetchval("SELECT current_database()")
        if identity != args.db_identity or database != args.database:
            raise ValueError("database identity mismatch")
        if not args.apply:
            return {"apply": False, "video_id": str(args.video_id), "action": args.action}
        playback = PlaybackService.configured(pool, settings)
        projection = LibraryProjection(
            pool,
            root=Path(settings.library_projection_dir),
            artwork_root=Path(settings.library_artwork_dir),
            base_url=settings.playback_public_url,
        )
        if args.action == "prepare":
            await playback.prepare(args.video_id)
        elif args.action == "publish":
            await projection.publish(args.video_id, args.poster)
        elif args.action == "evict-cache":
            await playback.evict(args.video_id)
        elif args.action == "reconcile":
            await projection.withdraw_invalid(args.video_id)
            rows = await pool.fetch(
                "SELECT video_id FROM library_publications WHERE state='PUBLISHED' AND video_id=$1", args.video_id
            )
            for row in rows:
                await projection.publish(row["video_id"])
        return {"apply": True, "video_id": str(args.video_id), "action": args.action, "status": "completed"}
    finally:
        await pool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "publish", "evict-cache", "reconcile"])
    parser.add_argument("--video-id", type=UUID, required=True)
    parser.add_argument("--db-identity", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--poster", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args))))
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
