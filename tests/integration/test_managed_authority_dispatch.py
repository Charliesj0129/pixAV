"""Managed authority must leave legacy tasks and queues untouched."""

import asyncio
import os
import secrets
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import pytest

from pixav.config import Settings
from pixav.maxwell_core import worker
from pixav.pixel_injector import storage_worker
from pixav.pixel_injector.windows_control import WindowsCapacityProbe
from pixav.shared.db import create_pool
from pixav.shared.disk import PhysicalCapacity
from scripts.provision_workflow_roles import _provision

pytestmark = pytest.mark.integration


async def test_managed_authority_preserves_legacy_pending_task(
    integration_db, integration_redis, tmp_path, monkeypatch
):
    for path in sorted(Path("migrations").glob("*.sql")):
        await integration_db.execute(path.read_text())
    login = "pixav_test_authority_" + uuid.uuid4().hex
    password = secrets.token_hex(24)
    await _provision(integration_db, name=login, group="pixav_execution_authority", password=password, exists=False)
    parsed = urlsplit(os.environ["PIXAV_E2E_ADMIN_DSN"])
    database = await integration_db.fetchval("SELECT current_database()")
    identity = str(await integration_db.fetchval("SELECT system_identifier FROM pg_control_system()"))
    redis, prefix = integration_redis
    video = await integration_db.fetchval("INSERT INTO videos(title) VALUES('synthetic legacy hold') RETURNING id")
    task = await integration_db.fetchval(
        "INSERT INTO tasks(video_id,queue_name) VALUES($1,$2) RETURNING id", video, prefix + ":download"
    )
    token = tmp_path / "pause-token"
    token.write_text(secrets.token_hex(32))
    token.chmod(0o600)
    settings = Settings(
        db_host=parsed.hostname,
        db_port=parsed.port,
        db_name=database,
        db_user=login,
        db_password=password,
        expected_db_identity=identity,
        expected_db_name=database,
        managed_media_workflow=True,
        pixel_injector_mode="windows_android",
        storage_worker_owner=str(uuid.uuid4()),
        storage_windows_runtime_id=str(uuid.uuid4()),
        windows_control_docker_daemon_id="synthetic-daemon",
        windows_control_identity_sha256="a" * 64,
        windows_capacity_volume_device=123,
        download_pause_token_file=str(token),
        download_dir=str(tmp_path),
        download_min_free_bytes=0,
        download_min_free_percent=0,
        library_reconcile_seconds=0,
        remote_reverify_interval_days=0,
        queue_crawl=prefix + ":crawl",
        queue_download=prefix + ":download",
        queue_upload=prefix + ":upload",
        download_pause_key=prefix + ":download-pause",
        system_pause_key=prefix + ":pause",
    )
    authority = await create_pool(settings)
    daemon = MagicMock()
    daemon.info.return_value = {"ID": "synthetic-daemon"}
    native = SimpleNamespace(docker=daemon, control=MagicMock())

    async def opened_pool(_settings):
        return authority

    async def opened_redis(_settings):
        return redis

    completed = asyncio.Event()

    async def tick_completed(*_args, **_kwargs):
        completed.set()

    monkeypatch.setattr(worker, "create_pool", opened_pool)
    monkeypatch.setattr(worker, "create_redis", opened_redis)
    monkeypatch.setattr(worker, "_publish_queue_depths", tick_completed)
    monkeypatch.setattr(storage_worker, "windows_runtime", lambda *_a, **_k: native)
    # This test covers real domain/queue dispatch; the native contract has a
    # separate live receipt and only its capacity boundary is synthetic here.
    monkeypatch.setattr(WindowsCapacityProbe, "_read", lambda _: PhysicalCapacity("D:\\", 1000, 500))
    running = asyncio.create_task(worker.run_loop(settings, interval=1))
    completion = asyncio.create_task(completed.wait())
    try:
        done, _ = await asyncio.wait([running, completion], timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if running in done:
            await running
        assert completed.is_set(), "the managed authority must complete a scheduling tick"
        assert await integration_db.fetchval("SELECT state FROM tasks WHERE id=$1", task) == "pending"
        assert await redis.llen(prefix + ":download") == 0
    finally:
        running.cancel()
        completion.cancel()
        try:
            await running
        except asyncio.CancelledError:
            pass
        await redis.delete(
            prefix + ":download", prefix + ":download:processing", prefix + ":crawl", prefix + ":crawl:processing"
        )
