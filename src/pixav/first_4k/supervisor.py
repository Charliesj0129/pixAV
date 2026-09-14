"""One-shot operator continuation for exactly one authorized retained run.

This is private operational material, not a queue, a daemon, or an execution
SSOT: every stage still comes from PostgreSQL, and nothing here retries a
challenge, a failed operation or a recovery drill. It exists because the run
spans days and pauses at part boundaries, and someone has to re-enter the CLI
at each one without re-deciding anything.

It supersedes four near-identical copies that lived under `.verify/`, which
differed only in a Redis fingerprint the host changed on reboot and in a file
lock check added later. Those copies could not see each other; this one takes
its run id, its expected instance and its evidence directory as arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, cast

import redis.asyncio as aioredis

import docker
from pixav.pixel_injector.canary import private_directory, read_private, single_flight
from pixav.shared.video_parts import VideoPartRepository

from .boundary import Expectation, Supervision, backup_boundary
from .contracts import read_status
from .evidence import command, event, source_digests, stamp, verify_sources, write
from .guards import checked_database
from .settings import PROJECT, REDIS, ROOT, WORK, container

ADVISORY_LOCK = 410041004
POLL_SECONDS = 60
ACCEPTANCE_KEYS = ("get", "head", "range", "suffix", "416", "vlc")


def load_expectation(path: Path) -> Expectation:
    """Read the authorized instance fingerprints from a private 0600 file."""
    return Expectation.model_validate(read_private(path))


async def identity(client: Any, expectation: Expectation) -> Any:
    """Open a connection only if every piece of the isolated stack is the authorized one."""
    if (await asyncio.to_thread(client.info))["ID"] != expectation.docker_id:
        raise RuntimeError("Docker daemon changed")
    context = (await asyncio.to_thread(command, ["docker", "context", "show"])).decode().strip()
    if context != "default":
        raise RuntimeError("Docker context changed")
    conn = await checked_database(client)
    redis = aioredis.from_url(REDIS)
    try:
        actual = str(await conn.fetchval("SELECT system_identifier FROM pg_control_system()"))
        run_id = (await redis.info("server"))["run_id"]
        if actual != expectation.db_identity or run_id != expectation.redis_identity:
            raise RuntimeError("DB or Redis instance changed")
        for role, volume in [("postgres", "movie_postgres"), ("redis", "movie_redis")]:
            item = container(client, role)
            mounts = [m for m in item.attrs["Mounts"] if m["Type"] == "volume"]
            if len(mounts) != 1 or mounts[0]["Name"] != f"{PROJECT}_{volume}":
                raise RuntimeError("isolated persistence ownership changed")
        return conn
    except BaseException:
        await conn.close()
        raise
    finally:
        await redis.aclose()


async def invoke(supervision: Supervision, command_name: str, number: int) -> dict:
    """Re-enter the CLI once, from disk, with its output kept as private evidence."""
    verify_sources(supervision.digests)
    log = supervision.out / f"{number:02d}-{command_name}.log"
    args = [
        str(ROOT / ".venv/bin/python"),
        "scripts/first_4k_movie.py",
        command_name,
        "--run-id",
        str(supervision.run_id),
    ]
    if command_name == "resume":
        args += ["--max-parts", "0"]
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        process = await asyncio.create_subprocess_exec(*args, stdout=handle, stderr=handle)
        event(supervision.out, "CLI_STARTED", command=command_name, pid=process.pid)
        returncode = await process.wait()
    lines = log.read_text().splitlines()
    result = json.loads(lines[-1]) if lines else {}
    event(supervision.out, "CLI_FINISHED", command=command_name, returncode=returncode, status=result.get("status"))
    return result


async def _wait_for_drill_boundary(client: Any, supervision: Supervision) -> None:
    """Wait out the running CLI, then capture the first-confirmed-part boundary."""
    while True:
        conn = await identity(client, supervision.expectation)
        try:
            status = await read_status(conn, supervision.run_id)
            available = await conn.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK)
            # The advisory lock alone is not enough: a runner that has not yet
            # reached the database still holds the single-flight file lock.
            file_available = True
            try:
                with single_flight():
                    pass
            except BlockingIOError:
                file_available = False
            if not available or not file_available:
                if status.get("liveness") != "RECENT":
                    raise RuntimeError("active runner heartbeat stale; retain for inspection")
                event(
                    supervision.out,
                    "WAITING_EXISTING_RUNNER",
                    stage=status["stage"],
                    parts=status["parts"],
                    confirmed=status["confirmed"],
                )
            else:
                state = await _document(conn, supervision.run_id)
                if state.get("stage") != "upload_paused":
                    raise RuntimeError("runner exited before the first confirmed-part boundary; inspect retained run")
                await backup_boundary(client, conn, state, "before-drill", supervision)
                return
        finally:
            await conn.close()
        await asyncio.sleep(POLL_SECONDS)


async def _document(conn: Any, run_id: uuid.UUID) -> dict:
    return json.loads(await conn.fetchval("SELECT document FROM first_4k_runs WHERE id=$1", run_id))


async def _resume_until_finished(client: Any, supervision: Supervision) -> dict:
    """Resume repeatedly, sleeping out each quota wait against the database clock."""
    number = 2
    while True:
        conn = await identity(client, supervision.expectation)
        await conn.close()
        result = await invoke(supervision, "resume", number)
        if result.get("status") != "QUOTA_WAIT":
            return result
        while True:
            conn = await identity(client, supervision.expectation)
            try:
                delay = await conn.fetchval(
                    "SELECT greatest(0,extract(epoch FROM next_action_at-now())) FROM first_4k_runs WHERE id=$1",
                    supervision.run_id,
                )
                if delay is None:
                    raise RuntimeError("quota checkpoint missing DB deadline")
                if delay <= 0:
                    break
                event(supervision.out, "QUOTA_WAIT", seconds=float(delay))
            finally:
                await conn.close()
            await asyncio.sleep(min(POLL_SECONDS, float(delay)))
        number += 1


def _seeks(duration: float, parts: list) -> list[float]:
    return sorted(
        {
            0.0,
            duration / 2,
            max(0, duration - 5),
            *(max(0, p.start_seconds - 1) for p in parts[1:]),
            *(min(duration - 1, p.start_seconds + 1) for p in parts[1:]),
        }
    )


async def _hand_to_operator(conn: Any, supervision: Supervision) -> None:
    """Record what a human still has to watch, then open the film on Windows.

    Headless VLC acceptance is already recorded by the CLI; picture, audio and
    seek on a real desktop player are the parts no automation here can assert,
    so they are written as OPEN rather than assumed.
    """
    state = await _document(conn, supervision.run_id)
    parts = await VideoPartRepository(conn).list(uuid.UUID(state["video_id"]))
    acceptance = state.get("playback_acceptance", {})
    if not all(acceptance.get(key) == "PASS" for key in ACCEPTANCE_KEYS):
        raise RuntimeError("final HTTP/headless VLC evidence incomplete")
    if len(parts) < 2 or any(p.usage_counted_at is None for p in parts):
        raise RuntimeError("final manifest incomplete")
    duration = state["source_provenance"]["reference"]["duration"]
    url = f"http://127.0.0.1:28000/stream/{state['video_id']}"
    write(
        supervision.out,
        "windows-playback-pending.json",
        {
            "at": stamp(),
            "url": url,
            "duration": duration,
            "strm": str(WORK / "playback" / (state["video_id"] + ".strm")),
            "required_seeks": _seeks(duration, parts),
            "windows_picture": "OPEN",
            "windows_audio": "OPEN",
            "windows_seek": "OPEN",
            "headless_vlc": "PASS",
            "quota": state["gates"]["quota"],
            "production_promotion": "NOT_REQUESTED",
        },
    )
    expectation = supervision.expectation
    if not expectation.windows_vlc_sha256:
        event(supervision.out, "WINDOWS_VLC_NOT_PINNED_MANUAL_LAUNCH_REQUIRED", url=url)
        return
    script = (
        "$exe=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) "
        f"'{expectation.windows_vlc_path}'; "
        "if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -ne "
        f"'{expectation.windows_vlc_sha256}') {{throw 'VLC identity mismatch'}}; "
        "Start-Process -FilePath $exe -ArgumentList "
        f"@('--no-one-instance','--no-video-title-show','{url}')"
    )
    await asyncio.to_thread(
        command,
        [
            "/init",
            "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; " + script,
        ],
    )
    event(supervision.out, "WINDOWS_VLC_LAUNCHED_OBSERVATION_REQUIRED", url=url)


async def supervise(args: argparse.Namespace, out: Path) -> dict:
    """Carry one authorized run from its current pause through to a playable film."""
    supervision = Supervision(
        run_id=args.run_id,
        expectation=load_expectation(args.expect),
        out=out,
        digests=source_digests(ROOT),
        authorization=args.authorization,
    )
    # The repository's own docker/ directory shadows the distribution for mypy,
    # which canary.py and redroid.py already work around this way.
    client = cast(Any, docker).from_env()
    try:
        conn = await identity(client, supervision.expectation)
        try:
            status = await read_status(conn, supervision.run_id)
            event(
                supervision.out,
                "CHECKED",
                stage=status.get("stage"),
                liveness=status.get("liveness"),
                parts=status.get("parts"),
                confirmed=status.get("confirmed"),
            )
        finally:
            await conn.close()
        if args.check:
            return {"status": "SUPERVISION_CHECKED", **{k: status.get(k) for k in ("stage", "liveness", "confirmed")}}
        write(
            supervision.out,
            "authorization.json",
            {
                "at": stamp(),
                "run_id": str(supervision.run_id),
                "backup_exception": supervision.authorization,
                "upload_concurrency": 1,
                "production_promotion": "NOT_REQUESTED",
            },
        )
        write(supervision.out, "source-digests.json", supervision.digests)

        await _wait_for_drill_boundary(client, supervision)
        if (await invoke(supervision, "recovery-drill", 1)).get("status") != "RECOVERY_DRILL_COMPLETE":
            raise RuntimeError("recovery drill did not complete; never retry automatically")

        conn = await identity(client, supervision.expectation)
        try:
            if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK):
                raise RuntimeError("another runner claimed post-drill boundary")
            await backup_boundary(client, conn, await _document(conn, supervision.run_id), "before-resume", supervision)
        finally:
            await conn.close()

        result = await _resume_until_finished(client, supervision)
        if result.get("status") not in {"PLAYABLE_QUOTA_WAIT", "LIVE_VERIFIED"}:
            raise RuntimeError("resume stopped; retain all artifacts and inspect private CLI log")
        conn = await identity(client, supervision.expectation)
        try:
            await _hand_to_operator(conn, supervision)
        finally:
            await conn.close()
        return {"status": "SUPERVISION_COMPLETE", "resume_status": result["status"]}
    finally:
        client.close()


def run(args: argparse.Namespace) -> dict:
    """Hold the supervision lock for the whole sequence, and never log exception text.

    A fresh evidence directory per supervision is deliberate: every file is
    written O_EXCL, so reusing one would fail on the first write rather than
    append to an earlier attempt's record.
    """
    out = private_directory(args.evidence or WORK / "evidence" / ("supervise-" + uuid.uuid4().hex))
    fd = os.open(out / "continuation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return asyncio.run(supervise(args, out))
    except Exception as exc:
        # Never expose external exception text or domain payloads to the console.
        event(out, "STOPPED_FOR_INSPECTION", error_type=type(exc).__name__)
        write(out, "failure.json", {"at": stamp(), "type": type(exc).__name__, "reason": str(exc)})
        return {"status": "SUPERVISION_STOPPED", "error_type": type(exc).__name__}
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(2)
