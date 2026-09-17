"""Real PostgreSQL manifest, restart, quota and restore contracts."""

# ruff: noqa: S603, S607 -- fixed guarded integration cluster commands
import asyncio
import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
import uvicorn

from pixav.shared.models import VideoPart
from pixav.shared.video_parts import VideoPartRepository
from pixav.strm_resolver.app import create_app
from pixav.strm_resolver.movie_acceptance import range_acceptance

pytestmark = pytest.mark.integration


async def migrate(pool, until):
    for path in sorted(Path("migrations").glob("*.sql")):
        if int(path.name.split("_")[0]) <= until:
            await pool.execute(path.read_text())


async def test_heartbeat_checks_original_postgres_lock_session(integration_db):
    from pixav.pixel_injector.canary import CanaryBlockedError
    from scripts.first_4k_contracts import RunHeartbeat

    pool = integration_db
    await migrate(pool, 11)
    run = uuid.uuid4()
    await pool.execute("INSERT INTO first_4k_runs(id,document) VALUES($1,'{}')", run)
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock(410041004)")
        heartbeat = RunHeartbeat(pool, run, {"stage": "synthetic"}, lock_connection=conn)
        try:
            await heartbeat.pulse()
            await conn.execute("SELECT pg_advisory_unlock(410041004)")
            with pytest.raises(CanaryBlockedError, match="advisory lock lost"):
                await heartbeat.pulse(transient_ok=True)
            with pytest.raises(CanaryBlockedError):
                heartbeat.check()
        finally:
            await conn.execute("SELECT pg_advisory_unlock_all()")


def parts_for(video):
    return [
        VideoPart(
            video_id=video,
            part_index=i,
            manifest_version=1,
            start_seconds=i * 10,
            end_seconds=(i + 1) * 10,
            size_bytes=1000,
            sha256=str(i + 1) * 64,
            filename=f"pixav-{video}-part-{i:06d}-{str(i + 1) * 16}.mp4",
        )
        for i in range(2)
    ]


async def test_movie_acceptance_over_http_with_postgres(integration_db, tmp_path):
    """Synthetic bytes, real repository and TCP redirects/ranges, no cloud claim."""
    pool = integration_db
    await migrate(pool, 10)
    video = await pool.fetchval("INSERT INTO videos(title) VALUES('synthetic HTTP contract') RETURNING id")
    account = await pool.fetchval("INSERT INTO accounts(email) VALUES('http@example.invalid') RETURNING id")
    repo = VideoPartRepository(pool)
    parts = parts_for(video)
    await repo.install(video, parts, {"sha256": "a" * 64})
    movie = tmp_path / "synthetic.mp4"
    movie.write_bytes(bytes(range(256)) * 800)
    app = create_app(redis_url=None, db_dsn=None)
    app.state.db_pool = pool
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if task.done():
                    await task
                    pytest.fail("HTTP server stopped before startup")
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            base = f"http://127.0.0.1:{listener.getsockname()[1]}"
            async with httpx.AsyncClient() as client:
                for route in ("resolve", "stream", "local"):
                    response = await client.get(f"{base}/{route}/{video}")
                    assert response.status_code == 409
                    assert response.json()["detail"] == "segmented playback requires prepare-playback"
            for part in parts:
                await repo.journal(part, "upload_intent", {}, account_id=account)
                await repo.confirm_backup(
                    part,
                    f"https://photos.app.goo.gl/synthetic-part-{part.part_index}",
                    {"backed_up": True, "original_quality": True},
                )
                await repo.confirm_original(
                    part, {"sha256": part.sha256, "size": part.size_bytes, "method": "photos-original-browser"}
                )
            await repo.publish(
                video, str(movie), {"content": "PASS", "cold_inputs": "photos-only", "size": movie.stat().st_size}
            )
            result = await range_acceptance(base, str(video), movie)
            assert all(result[key] == "PASS" for key in ("get", "head", "range", "suffix", "416"))
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)


async def test_manifest_restart_quota_and_cloud_publication(integration_db):
    pool = integration_db
    await migrate(pool, 10)
    await pool.execute(Path("migrations/010_video_parts.sql").read_text())
    video = await pool.fetchval("INSERT INTO videos(title) VALUES('manifest contract') RETURNING id")
    account = await pool.fetchval(
        "INSERT INTO accounts(email,daily_quota_bytes) VALUES('fixture@example.invalid',2500) RETURNING id"
    )
    repo = VideoPartRepository(pool)
    parts = parts_for(video)
    await repo.install(video, parts, {"sha256": "a" * 64})
    await repo.install(video, parts, {"sha256": "a" * 64})
    with pytest.raises(ValueError, match="immutable"):
        await repo.install(video, [parts[0].model_copy(update={"sha256": "f" * 64}), parts[1]], {})
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("UPDATE video_parts SET size_bytes=10000000000 WHERE video_id=$1", video)
    evidence = {"backed_up": True, "original_quality": True}
    await repo.journal(parts[0], "upload_intent", {"guest_id": "retained", "share_intent": True}, account_id=account)
    await asyncio.gather(
        *(repo.confirm_backup(parts[0], "https://photos.app.goo.gl/first", evidence) for _ in range(3))
    )
    assert await pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == 1000
    await repo.journal(parts[1], "reconcile", {"guest_id": "retained", "media_id": "2"}, account_id=account)
    recovered = await VideoPartRepository(pool).list(video)
    assert recovered[0].usage_counted_at is not None
    assert recovered[1].recovery["guest_id"] == "retained"
    with pytest.raises(ValueError, match="incomplete"):
        await repo.publish(video, "/playback/complete.mp4", {"content": "PASS", "cold_inputs": "photos-only"})
    # PostgreSQL clock rollover; retrying the first success must not debit again.
    await pool.execute("UPDATE accounts SET quota_reset_at=now()-interval '1 second' WHERE id=$1", account)
    await repo.confirm_backup(parts[0], "https://photos.app.goo.gl/first", evidence)
    await repo.confirm_backup(parts[1], "https://photos.app.goo.gl/second", evidence)
    assert await pool.fetchval("SELECT daily_uploaded_bytes FROM accounts WHERE id=$1", account) == 1000
    with pytest.raises(ValueError, match="identity"):
        await repo.confirm_original(parts[0], {"sha256": "wrong", "size": 1000})
    for part in parts:
        await repo.confirm_original(
            part, {"sha256": part.sha256, "size": part.size_bytes, "method": "photos-original-browser"}
        )
    await repo.publish(video, "/playback/complete.mp4", {"content": "PASS", "cold_inputs": "photos-only", "size": 2000})
    await repo.publish(video, "/playback/complete.mp4", {"content": "PASS", "cold_inputs": "photos-only", "size": 2000})
    assert await pool.fetchval("SELECT count(*) FROM videos WHERE local_path IS NOT NULL") == 1
    parent = await pool.fetchrow("SELECT * FROM videos WHERE id=$1", video)
    assert parent["share_url"] is None
    assert parent["manifest_version"] == parent["playback_manifest_version"] == 1
    assert parent["expected_part_count"] == 2
    assert json.loads(parent["metadata_json"])["segmented_playback"]["content"] == "PASS"


async def test_parts_dump_restores_with_parent_and_account(integration_db, tmp_path):
    """Restore a full dump into a second fresh database on the guarded test cluster."""
    pool = integration_db
    await migrate(pool, 10)
    name = await pool.fetchval("SELECT current_database()")
    video = await pool.fetchval("INSERT INTO videos(title) VALUES('restore contract') RETURNING id")
    repo = VideoPartRepository(pool)
    await repo.install(video, parts_for(video), {"sha256": "b" * 64})
    target = "pixav_test_" + uuid.uuid4().hex
    admin = await asyncpg.connect(
        os.getenv("PIXAV_E2E_ADMIN_DSN", "postgresql://pixav_test:integration-only@127.0.0.1:15432/pixav_integration")
    )
    from scripts.integration_guard import require_test_database

    await require_test_database(admin)
    await admin.execute(f'CREATE DATABASE "{target}"')
    try:
        dump = subprocess.run(
            ["docker", "exec", "pixav-integration-postgres-1", "pg_dump", "-U", "pixav_test", "-Fc", name],
            capture_output=True,
            check=True,
            timeout=60,
        )  # noqa: S603,S607
        backup = tmp_path / "full.dump"
        backup.write_bytes(dump.stdout)
        backup.chmod(0o600)
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "pixav-integration-postgres-1",
                "pg_restore",
                "-U",
                "pixav_test",
                "-d",
                target,
                "--exit-on-error",
            ],
            input=dump.stdout,
            capture_output=True,
            check=True,
            timeout=60,
        )  # noqa: S603,S607
        restored = await asyncpg.connect(
            host="127.0.0.1", port=15432, user="pixav_test", password="integration-only", database=target
        )
        try:
            assert await restored.fetchval("SELECT count(*) FROM video_parts WHERE video_id=$1", video) == 2
            assert await restored.fetchval("SELECT expected_part_count FROM videos WHERE id=$1", video) == 2
        finally:
            await restored.close()
    finally:
        await require_test_database(admin)
        await admin.execute(f'DROP DATABASE "{target}"')
        await admin.close()


async def test_manifest_commit_before_run_checkpoint_recovery(integration_db, tmp_path, monkeypatch):
    """A new flow restores the committed manifest without invoking media tools."""
    from argparse import Namespace
    from unittest.mock import Mock

    import scripts.first_4k_movie as module
    from pixav.media_loader.video_parts import sha256

    pool = integration_db
    await migrate(pool, 11)
    video = await pool.fetchval(
        "INSERT INTO videos(title,info_hash) VALUES('recovery fixture',$1) RETURNING id", "a" * 40
    )
    source = tmp_path / "downloads" / ("a" * 40) / "fixture.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic source")
    directory = tmp_path / "parts" / str(video)
    directory.mkdir(parents=True)
    (tmp_path / "evidence").mkdir()
    parts = parts_for(video)
    for i, part in enumerate(parts):
        path = directory / part.filename
        path.write_bytes(bytes([i]) * 1000)
        parts[i] = part.model_copy(update={"sha256": sha256(path)})
    provenance = {
        "size": source.stat().st_size,
        "sha256": sha256(source),
        "local_merge": "PASS",
        "reference": {"duration": 20},
        "discovery": {"info_hash": "a" * 40},
    }
    # This is the checkpoint from before install(); no prepared state was saved.
    state = {
        "id": str(uuid.uuid4()),
        "stage": "downloading",
        "candidates": [{"video_id": str(video), "info_hash": "a" * 40, "source": str(source)}],
    }
    await pool.execute(
        "INSERT INTO first_4k_runs(id,document) VALUES($1,$2::jsonb)", uuid.UUID(state["id"]), json.dumps(state)
    )

    # Commit in a separate process, then terminate without a run checkpoint.
    admin_dsn = os.getenv(
        "PIXAV_E2E_ADMIN_DSN", "postgresql://pixav_test:integration-only@127.0.0.1:15432/pixav_integration"
    )
    parsed = urlsplit(admin_dsn)
    database = await pool.fetchval("SELECT current_database()")
    code = """
import asyncio, json, os, sys, uuid
import asyncpg
from pixav.shared.models import VideoPart
from pixav.shared.video_parts import VideoPartRepository
async def main():
    data = json.load(sys.stdin)
    pool = await asyncpg.create_pool(data['dsn'], min_size=1, max_size=1)
    await VideoPartRepository(pool).install(uuid.UUID(data['video']), [VideoPart.model_validate(p) for p in data['parts']], data['provenance'])
    os._exit(77)
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(
        process.communicate(
            json.dumps(
                {
                    "dsn": urlunsplit(parsed._replace(path=f"/{database}")),
                    "video": str(video),
                    "parts": [p.model_dump(mode="json") for p in parts],
                    "provenance": provenance,
                }
            ).encode()
        ),
        30,
    )
    assert process.returncode == 77
    persisted = json.loads(
        await pool.fetchval("SELECT document FROM first_4k_runs WHERE id=$1", uuid.UUID(state["id"]))
    )
    assert persisted == state and "video_id" not in persisted
    state = persisted
    monkeypatch.setattr(module, "WORK", tmp_path)
    flow = module.MovieFlow(
        Namespace(images=Namespace(get=lambda _: Namespace(id="fixture"))), pool, Namespace(), state
    )
    flow.media.prepare = Mock(side_effect=AssertionError("must not segment again"))
    assert await flow.recover_prepared()
    assert state["stage"] == "prepared"
    assert await flow.recover_prepared()
    assert await pool.fetchval("SELECT count(*) FROM videos") == 1
    assert await pool.fetchval("SELECT count(*) FROM video_parts") == 2
    (directory / parts[0].filename).write_bytes(b"damaged")
    with pytest.raises(module.CanaryBlockedError, match="recovery failed"):
        await flow.recover_prepared()
    assert (directory / parts[0].filename).read_bytes() == b"damaged"


async def test_status_is_readonly_before_and_after_heartbeat_migration(integration_db):
    from scripts.first_4k_contracts import RunHeartbeat, read_status

    pool = integration_db
    await migrate(pool, 10)
    run = uuid.uuid4()
    state = {"id": str(run), "stage": "prepared", "private": "must-not-leak"}
    await pool.execute("INSERT INTO first_4k_runs(id,document) VALUES($1,$2::jsonb)", run, json.dumps(state))
    async with pool.acquire() as conn:
        result = await read_status(conn, run)
    assert result["liveness"] == "UNKNOWN"
    assert "must-not-leak" not in json.dumps(result)
    await pool.execute(Path("migrations/011_first_4k_heartbeat.sql").read_text())
    await pool.execute(Path("migrations/011_first_4k_heartbeat.sql").read_text())
    hb = RunHeartbeat(pool, run, state)
    await hb.pulse()
    await pool.execute(
        "UPDATE first_4k_runs SET document=$2::jsonb WHERE id=$1", run, json.dumps({**state, "stage": "uploaded"})
    )
    async with pool.acquire() as conn:
        result = await read_status(conn, run)
    assert result["liveness"] == "RECENT" and result["heartbeat_at"]
    await pool.execute("UPDATE first_4k_runs SET heartbeat_at=now()-interval '121 seconds' WHERE id=$1", run)
    async with pool.acquire() as conn:
        assert (await read_status(conn, run))["liveness"] == "STALE"
    for timestamp in ("2000-01-01T00:00:00.1+00:00", "2000-01-01T00:00:00.12345+00:00"):
        await pool.execute("UPDATE first_4k_runs SET heartbeat_at=$2::text::timestamptz WHERE id=$1", run, timestamp)
        async with pool.acquire() as conn:
            assert (await read_status(conn, run))["liveness"] == "STALE"
