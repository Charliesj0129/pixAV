"""Real pg_dump/pg_restore and lossless selected-row/queue restoration drill."""

import json
import subprocess
from pathlib import Path

import pytest

from pixav.shared.queue import TaskQueue
from scripts.backup_files import create_backup_file
from scripts.instance_guard import database_identity, write_backup_metadata
from scripts.phase0_backup import capture, restore_empty_test_database, save_selected, validate_selected

pytestmark = pytest.mark.integration


def postgres_tool(*args, payload=None):
    return subprocess.run(  # noqa: S603 -- fixed Compose project and postgres executable arguments
        ["docker", "compose", "-f", "docker-compose.integration.yml", "exec", "-T", "postgres", *args],  # noqa: S607
        input=payload,
        capture_output=True,
        check=True,
    ).stdout


async def test_lossless_backup_and_full_restore(integration_db, integration_redis, tmp_path):
    pool = integration_db
    redis, key = integration_redis
    for path in sorted(Path("migrations").glob("*.sql")):
        await pool.execute(path.read_text())
    account = await pool.fetchval(
        "INSERT INTO accounts(email,password) VALUES('fixture@example.invalid','private-fixture') RETURNING id"
    )
    storage = await pool.fetchval("INSERT INTO storage_instances(account_id) VALUES($1) RETURNING id", account)
    await pool.execute("UPDATE accounts SET storage_instance_id=$1 WHERE id=$2", storage, account)
    video = await pool.fetchval(
        "INSERT INTO videos(title,metadata_json) VALUES('restore fixture', '{\"provenance\":{\"original\":true}}') RETURNING id"
    )
    task = await pool.fetchval(
        "INSERT INTO tasks(video_id,account_id,queue_name) VALUES($1,$2,$3) RETURNING id", video, account, key
    )
    await pool.execute("INSERT INTO source_candidates(video_id,magnet_uri) VALUES($1,'magnet:?fixture')", video)
    await pool.execute(
        "INSERT INTO task_replay_audit(task_id,previous_state,previous_retries) VALUES($1,'failed',3)", task
    )
    raw = b'{ "task_id": "' + str(task).encode() + b'", "private": "opaque", "unknown": [1,2] }'
    await redis.rpush(key, raw, b"second\x00payload")
    await redis.rpush(f"{key}:processing", b"claimed")
    await redis.set(f"{key}:unrelated", b"keep")
    target = dict(task_id=task, video_id=video, queue_name=key)
    original = await capture(pool, redis, **target)
    name = await pool.fetchval("SELECT current_database()")
    dump = tmp_path / "full.dump"
    with create_backup_file(dump, binary=True) as handle:
        handle.write(postgres_tool("pg_dump", "-U", "pixav_test", "-Fc", "-d", name))
    write_backup_metadata(dump, system_identifier=await database_identity(pool), database=name)
    selected = tmp_path / "selected.json"
    await save_selected(selected, pool, redis, database_backup=dump, **target)
    assert selected.stat().st_mode & 0o777 == 0o600
    guards = dict(database_backup=dump, expected_queued=2, expected_processing=1, **target)
    await validate_selected(selected, pool, redis, **guards)
    with pytest.raises(RuntimeError, match="count mismatch"):
        await validate_selected(selected, pool, redis, **dict(guards, expected_queued=1))
    await redis.lpop(key)
    with pytest.raises(RuntimeError, match="stale"):
        await validate_selected(selected, pool, redis, **guards)
    await redis.lpush(key, raw)
    await pool.execute("UPDATE tasks SET retries=1 WHERE id=$1", task)
    with pytest.raises(RuntimeError, match="stale"):
        await validate_selected(selected, pool, redis, **guards)
    await pool.execute("DELETE FROM videos WHERE id=$1", video)
    await pool.execute("DELETE FROM accounts WHERE id=$1", account)
    await redis.delete(key, f"{key}:processing")
    for field, message in (("database_identity", "cluster identity"), ("redis_run_id", "Redis identity")):
        wrong = json.loads(selected.read_text())
        wrong["snapshot"][field] = "wrong-instance"
        wrong_path = tmp_path / f"wrong-{field}.json"
        with create_backup_file(wrong_path) as handle:
            json.dump(wrong, handle)
        with pytest.raises(RuntimeError, match=message):
            await restore_empty_test_database(wrong_path, pool, redis, queue_name=key)
        assert await pool.fetchval("SELECT count(*) FROM videos") == 0
        assert await redis.llen(key) == 0
    await restore_empty_test_database(selected, pool, redis, queue_name=key)
    assert await capture(pool, redis, **target) == original
    assert await redis.get(f"{key}:unrelated") == b"keep"
    with pytest.raises(RuntimeError, match="must be empty"):
        await restore_empty_test_database(selected, pool, redis, queue_name=key)
    # Independently prove the full custom-format backup can restore the same DB.
    postgres_tool(
        "pg_restore",
        "-U",
        "pixav_test",
        "--clean",
        "--if-exists",
        "--exit-on-error",
        "-d",
        name,
        payload=dump.read_bytes(),
    )
    assert await capture(pool, redis, **target) == original
    assert json.loads(selected.read_text())["snapshot"]["rows"]["accounts"][0]["password"] == "private-fixture"


async def test_processing_recovery_fifo_and_owned_cleanup(integration_redis):
    redis, key = integration_redis
    await redis.set(f"{key}:unrelated", b"keep")
    queue = TaskQueue(redis, key)
    await queue.push({"order": 1})
    await queue.push({"order": 2})
    first, raw = await queue.pop_claim(timeout=1)
    assert first == {"order": 1}
    assert await queue.requeue_inflight() == 1
    first, raw = await queue.pop_claim(timeout=1)
    assert first == {"order": 1}
    await queue.ack(raw)
    second, raw = await queue.pop_claim(timeout=1)
    assert second == {"order": 2}
    await queue.ack(raw)
    await redis.delete(key, queue.processing_name)
    assert await redis.get(f"{key}:unrelated") == b"keep"
