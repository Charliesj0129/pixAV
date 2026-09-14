"""Identity and capacity checks the run refuses to start without.

They live outside cli.py because the supervisor needs them too, and cli.py
imports the supervisor for its `supervise` command.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import asyncpg
import redis.asyncio as aioredis

from pixav.media_loader.video_parts import disk_budget
from pixav.pixel_injector.canary import CanaryBlockedError, private_directory
from pixav.pixel_injector.profiles import get_profile
from pixav.shared.instance import database_identity, redis_identity

from .settings import DSN, MEDIA, PROJECT, REDIS, ROOT, TOOLS, WORK, container, now


async def preflight(client: Any, args: argparse.Namespace) -> dict:
    for name in ("downloads", "parts", "playback", "evidence", "guest"):
        private_directory(WORK / name)
    db = container(client, "postgres")
    redis_container = container(client, "redis")
    for service, name in ((db, "movie_postgres"), (redis_container, "movie_redis")):
        mounts = [m for m in service.attrs["Mounts"] if m.get("Type") == "volume"]
        if len(mounts) != 1 or mounts[0].get("Name") != f"{PROJECT}_{name}":
            raise CanaryBlockedError("database persistence is not owned by the isolated project")
    for service, destination in (("qbittorrent", "/downloads"), ("qbittorrent", "/config")):
        item = container(client, service)
        mounts = [m for m in item.attrs["Mounts"] if m["Destination"] == destination]
        expected = WORK / ("downloads" if destination == "/downloads" else "qbit-config")
        if len(mounts) != 1 or Path(mounts[0]["Source"]) != expected:
            raise CanaryBlockedError("qBit mount is not the isolated owned directory")
    conn = await asyncpg.connect(DSN)
    redis = aioredis.from_url(REDIS)
    try:
        identity = await database_identity(conn)
        observed = db.exec_run(
            [
                "psql",
                "-U",
                "pixav_test",
                "-d",
                "pixav_first_4k",
                "-Atc",
                "SELECT system_identifier FROM pg_control_system()",
            ]
        )
        if (
            observed.exit_code
            or identity != observed.output.decode().strip()
            or await conn.fetchval("SELECT current_database()") != "pixav_first_4k"
        ):
            raise CanaryBlockedError("PostgreSQL instance identity mismatch")
        redis_id = await redis_identity(redis)
        observed_redis = redis_container.exec_run(["redis-cli", "INFO", "server"])
        if observed_redis.exit_code or f"run_id:{redis_id}" not in observed_redis.output.decode():
            raise CanaryBlockedError("Redis instance identity mismatch")
    finally:
        await redis.aclose()
        await conn.close()
    profile = get_profile("gphotos_pixel_xl_v1", path=ROOT / "config/android_profiles.yml")
    for image in (profile.image, MEDIA, TOOLS):
        client.images.get(image)
    # Preflight checks the latch, not allocations already made by an earlier run.
    # Each allocating stage reserves its own remaining peak below.
    space = disk_budget(
        [
            (WORK / "downloads", 0),
            (WORK / "parts", 0),
            (WORK / "playback", 0),
            (WORK / "guest", 0),
        ]
    )
    if not all(item["ready"] for item in space):
        raise CanaryBlockedError("peak media allocation would cross 100 GiB / 10 percent disk latch")
    return {"db_identity": identity, "redis_identity": redis_id, "disk": space, "at": now(), "vpn": "OPEN"}


async def checked_database(client: Any) -> Any:
    """Read-only identity check usable with stopped non-DB dependencies."""
    db = await asyncio.to_thread(container, client, "postgres")
    mounts = [m for m in db.attrs["Mounts"] if m.get("Type") == "volume"]
    if len(mounts) != 1 or mounts[0].get("Name") != f"{PROJECT}_movie_postgres":
        raise CanaryBlockedError("isolated PostgreSQL volume mismatch")
    conn = await asyncpg.connect(DSN)
    try:
        observed = await asyncio.to_thread(
            db.exec_run,
            [
                "psql",
                "-U",
                "pixav_test",
                "-d",
                "pixav_first_4k",
                "-Atc",
                "SELECT system_identifier FROM pg_control_system()",
            ],
        )
        if (
            observed.exit_code
            or observed.output.decode().strip() != await database_identity(conn)
            or await conn.fetchval("SELECT current_database()") != "pixav_first_4k"
        ):
            raise CanaryBlockedError("PostgreSQL instance identity mismatch")
        return conn
    except BaseException:
        await conn.close()
        raise
