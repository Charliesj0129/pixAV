"""Single entry point for the isolated single-film pipeline."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import asyncpg

import docker
from pixav.config import get_settings
from pixav.media_loader.video_parts import TARGET_BYTES, MediaOperationError, sha256
from pixav.pixel_injector.canary import CanaryBlockedError, private_directory, single_flight, write_private
from pixav.pixel_injector.maestro_parts import UserActionRequiredError
from pixav.pixel_injector.profiles import get_profile
from pixav.shared.migrations import run_migrations

from .contracts import RunHeartbeat, read_status, resolve_configuration
from .discovery import collect_boards
from .flow import MovieFlow
from .guards import checked_database, preflight
from .recovery import drill
from .settings import (
    DSN,
    MEDIA,
    ROOT,
    SELECTION_VERSION,
    SUCCESS_STATUSES,
    TOOLS,
    WORK,
    container,
    now,
)
from .supervisor import run as supervise


def runtime_configuration(client: Any) -> dict:
    profile = get_profile("gphotos_pixel_xl_v1", path=ROOT / "config/android_profiles.yml")
    return {
        "profile": profile.model_dump(mode="json"),
        "images": {image: client.images.get(image).id for image in (profile.image, MEDIA, TOOLS)},
        "selection_version": SELECTION_VERSION,
        "segmentation": {"version": 1, "target_bytes": TARGET_BYTES, "strategy": "min-target-half-source"},
    }


async def dispatch(flow: MovieFlow) -> dict:  # noqa: C901 - explicit command and gate dispatch
    args, state, pool = flow.args, flow.state, flow.pool
    if args.command == "recovery-drill":
        return await drill(flow, WORK, ROOT)
    if args.command == "verify":
        return await flow.verify()
    if state.get("next_action_at") and state["stage"] == "quota_wait":
        clock = await pool.fetchval("SELECT now()")
        if clock < datetime.fromisoformat(state["next_action_at"]):
            return {"status": "QUOTA_WAIT", "resume_at": state["next_action_at"]}
        state.pop("next_action_at", None)
    if args.command != "prepare-playback":
        await flow.discover()
        await flow.download_prepare()
        await flow.upload()
        if state["stage"] == "uploaded":
            for invocation in state.get("cli_invocations", []):
                if invocation["id"] == state.get("invocation_id"):
                    invocation["uploads_completed_at"] = now()
            await flow.save()
        if state["stage"] == "quota_wait":
            return {"status": "QUOTA_WAIT", "resume_at": state["next_action_at"]}
        if state["stage"] == "upload_paused":
            return {"status": "PART_LIMIT_REACHED", "next_step": "resume"}
    await flow.playback()
    if flow.heartbeat:
        flow.heartbeat.check()
    process = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        "-f",
        str(ROOT / "docker-compose.first-4k.yml"),
        "up",
        "-d",
        "resolver",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if await asyncio.wait_for(process.wait(), 120):
        raise CanaryBlockedError("isolated resolver failed to start")
    return await flow.verify()


async def execute(args: argparse.Namespace) -> dict:  # noqa: C901 - guarded CLI dispatch
    client = cast(Any, docker).from_env()
    default_board = get_settings().crawl_seed_urls.split(",")[0].split("|")[0]
    try:
        if args.command == "boards":
            directory = private_directory(WORK / "evidence" / ("boards-" + uuid.uuid4().hex))
            return await collect_boards(
                argparse.Namespace(
                    board=args.board or default_board,
                    cookie_file=args.cookie_file,
                    flaresolverr="http://127.0.0.1:28191",
                    output=directory,
                )
            )
        conn = await checked_database(client)
        try:
            if args.command == "status":
                return await read_status(conn, args.run_id)
            if not await conn.fetchval("SELECT pg_try_advisory_lock(410041004)"):
                raise CanaryBlockedError("another single-film runner is active")
            rows = (
                await conn.fetch("SELECT document FROM first_4k_runs")
                if await conn.fetchval("SELECT to_regclass('public.first_4k_runs')")
                else []
            )
            if len(rows) > 1:
                raise CanaryBlockedError("multiple film runs require explicit reconciliation")
            if not rows and args.command in {"resume", "verify", "prepare-playback", "recovery-drill"}:
                raise CanaryBlockedError("no saved run to resume")
            state = (
                json.loads(rows[0]["document"])
                if rows
                else {
                    "id": str(uuid.uuid4()),
                    "stage": "new",
                    "gates": {
                        "movie": "OPEN",
                        "quota": "OPEN",
                        "automation": "OPEN",
                        "vpn": "OPEN",
                        "production_promotion": "NOT_REQUESTED",
                    },
                }
            )
            if args.run_id is not None and str(args.run_id) != state["id"]:
                raise CanaryBlockedError("requested run identity mismatch")
            # Configuration drift is rejected before mkdir, backup, migration or any
            # external effect. Reset and preflight do not reinterpret saved settings.
            if args.command not in {"reset", "preflight"}:
                configuration = resolve_configuration(
                    args, state, await asyncio.to_thread(runtime_configuration, client), default_board
                )
                state["configuration"] = configuration
            evidence = await preflight(client, args)
            if args.command == "preflight":
                write_private(WORK / "evidence/preflight.json", evidence)
                return {"status": "PREFLIGHT_READY", "vpn": "OPEN"}
            backup = WORK / "evidence" / ("before-migration-" + uuid.uuid4().hex + ".sql")
            db = container(client, "postgres")
            dump = await asyncio.to_thread(db.exec_run, ["pg_dump", "-U", "pixav_test", "pixav_first_4k"])
            if dump.exit_code:
                raise CanaryBlockedError("isolated full backup failed")
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(dump.output)
            write_private(
                backup.with_suffix(".meta.json"),
                {
                    "db_identity": evidence["db_identity"],
                    "sha256": sha256(backup),
                    "at": now(),
                },
            )
            await run_migrations(DSN, until="011_first_4k_heartbeat.sql")
            pool = await asyncpg.create_pool(DSN, min_size=2, max_size=5)
            try:
                flow = MovieFlow(client, pool, args, state)
                if args.command == "reset":
                    return await flow.reset(bool(rows))
                invocation = {"id": str(uuid.uuid4()), "command": args.command, "started_at": now()}
                state["invocation_id"] = invocation["id"]
                state.setdefault("cli_invocations", []).append(invocation)
                await flow.save()
                flow.heartbeat = RunHeartbeat(pool, flow.id, state, lock_connection=conn)
                flow.media.check = flow.check_operation
                async with flow.heartbeat.watch():
                    result = await dispatch(flow)
                    invocation.update(result=result["status"], completed_at=now())
                    await flow.save()
                    return result
            finally:
                await pool.close()
        finally:
            # Closing the dedicated connection also releases the advisory lock.
            await conn.close()
    finally:
        client.close()


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # "resume" is an alias of "run": recovery is driven by the persisted state
    # document, not by a different code path, so both re-enter at the same stage.
    parser.add_argument(
        "command",
        choices=(
            "boards",
            "preflight",
            "reset",
            "run",
            "resume",
            "verify",
            "prepare-playback",
            "status",
            "recovery-drill",
            "supervise",
        ),
    )
    parser.add_argument("--board")
    parser.add_argument("--run-id", type=uuid.UUID)
    parser.add_argument("--cookie-file", type=Path, default=ROOT / "secrets/sehuatang-cookies.txt")
    parser.add_argument("--google-secret", type=Path, default=ROOT / "secrets/google-photos-canary.json")
    parser.add_argument("--max-threads", type=int)
    parser.add_argument("--max-movie-gib", type=int)
    parser.add_argument("--min-movie-seconds", type=int)
    parser.add_argument("--min-movie-gib", type=int)
    # 0 means no limit. A positive value caps how many parts THIS invocation newly
    # confirms; parts already confirmed by an earlier run are skipped and do not count.
    parser.add_argument("--max-parts", type=nonnegative_int, default=0)
    # Off by default: the attachment endpoint answers this client with a
    # Cloudflare interstitial. Extra trackers reach the same swarms.
    parser.add_argument("--fetch-attachments", action=argparse.BooleanOptionalAction, default=None)
    # supervise: which run to carry, which instance it was authorised against,
    # and where its evidence goes. A fresh directory is created when omitted.
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--expect", type=Path, default=WORK / "evidence/expected-identity.json")
    parser.add_argument("--authorization", default="")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    try:
        # supervise re-enters this CLI from disk between stage boundaries, so it
        # stays outside single_flight(): holding the lock would block its own child.
        if args.command == "supervise":
            result = supervise(args)
        elif args.command == "status":
            result = asyncio.run(execute(args))
        else:
            with single_flight():
                result = asyncio.run(execute(args))
        print(json.dumps(result))
        return 0 if result["status"] in SUCCESS_STATUSES else 2
    except Exception as exc:
        result = {
            "status": "USER_ACTION_REQUIRED" if isinstance(exc, UserActionRequiredError) else "BLOCKED",
            "error_type": type(exc).__name__,
        }
        if isinstance(exc, MediaOperationError):
            result.update(operation=exc.operation, category=exc.category)
        if isinstance(exc, CanaryBlockedError):
            result["reason"] = str(exc)
        print(json.dumps(result))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
