"""Exercise real SQL, including expand/backfill and candidate lifecycle."""

from pathlib import Path

import pytest

from pixav.shared.models import Video
from pixav.shared.repository import SourceCandidateRepository, VideoRepository
from scripts.phase0_cohort import sample

pytestmark = pytest.mark.integration


async def migrate(pool, until=9):
    for path in sorted(Path("migrations").glob("*.sql")):
        if int(path.name.split("_")[0]) <= until:
            await pool.execute(path.read_text())


async def test_expand_contract_and_candidate_switch(integration_db):
    pool = integration_db
    await migrate(pool, 7)
    first = "magnet:?xt=urn:btih:" + "a" * 40
    second = "magnet:?xt=urn:btih:" + "b" * 40
    video_id = await pool.fetchval(
        "INSERT INTO videos(title, magnet_uri, info_hash, cdn_url, share_url) "
        "VALUES('fixture', $1, $2, 'expired', 'pixav-local://fixture') RETURNING id",
        first,
        "a" * 40,
    )
    expand = Path("migrations/008_source_candidates.sql").read_text()
    await pool.execute(expand)
    repo = SourceCandidateRepository(pool)
    candidate = await repo.next_candidate(video_id)
    assert candidate.magnet_uri == first
    await repo.mark_unavailable(video_id, first, cooldown_hours=6, reason="SourceUnavailableError")
    await pool.execute(expand)
    assert await pool.fetchval("SELECT count(*) FROM source_candidates") == 1
    assert await repo.next_candidate(video_id) is None
    assert await repo.release_expired_cooldowns() == 0
    await pool.execute(
        "INSERT INTO source_candidates(video_id, magnet_uri, info_hash, quality_score) VALUES($1,$2,$3,10)",
        video_id,
        second,
        "b" * 40,
    )
    assert (await repo.next_candidate(video_id, exclude_magnet=first)).magnet_uri == second
    await repo.mark_succeeded(video_id, second)
    await pool.execute(
        "UPDATE source_candidates SET unavailable_until=now()-interval '1 second' WHERE magnet_uri=$1", first
    )
    assert await repo.release_expired_cooldowns() == 1
    assert (await repo.next_candidate(video_id)).magnet_uri == second
    await pool.execute(Path("migrations/009_drop_video_cdn_url.sql").read_text())
    assert await pool.fetchval("SELECT share_url FROM videos WHERE id=$1", video_id) == "pixav-local://fixture"
    assert not await pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='videos' AND column_name='cdn_url')"
    )
    videos = VideoRepository(pool)
    inserted = await videos.insert(Video(title="contract insert"))
    assert (await videos.find_by_id(inserted.id)).title == "contract insert"


async def test_cohort_freezes_actual_open_tasks(integration_db, integration_redis):
    from types import SimpleNamespace

    pool = integration_db
    redis, key = integration_redis
    await migrate(pool)
    video = await pool.fetchval("INSERT INTO videos(title) VALUES('cohort') RETURNING id")
    task = await pool.fetchval("INSERT INTO tasks(video_id) VALUES($1) RETURNING id", video)
    await pool.execute("INSERT INTO tasks(video_id,state) VALUES($1,'complete')", video)
    settings = SimpleNamespace(queue_download=key, queue_upload=f"{key}:processing")
    initial = await sample(pool, redis, settings, None)
    assert [row["task_id"] for row in initial["tasks"]] == [str(task)]
    await pool.execute("INSERT INTO tasks(video_id) VALUES($1)", video)
    await pool.execute(
        "UPDATE tasks SET state='failed',error_message='SourceUnavailableError: secret' WHERE id=$1", task
    )
    later = await sample(pool, redis, settings, [task])
    assert len(later["tasks"]) == 1
    assert later["tasks"][0]["failure_class"] == "SourceUnavailableError"
    assert "secret" not in str(later)


async def test_cohort_process_kill_and_redis_restart(integration_db, integration_redis, tmp_path):
    """Opt-in disruption of only the dedicated Redis; retain the original T0 file."""
    import asyncio
    import json
    import os
    import subprocess
    import sys
    from types import SimpleNamespace

    from redis.exceptions import ConnectionError as RedisConnectionError

    from scripts.instance_guard import redis_identity
    from scripts.phase0_cohort import build_report, write_json

    if os.getenv("PIXAV_RUN_RESTART_DRILL") != "1":
        pytest.skip("set PIXAV_RUN_RESTART_DRILL=1 to restart isolated Redis")
    pool = integration_db
    redis, key = integration_redis
    await migrate(pool)
    video = await pool.fetchval("INSERT INTO videos(title) VALUES('interruption') RETURNING id")
    task = await pool.fetchval("INSERT INTO tasks(video_id) VALUES($1) RETURNING id", video)
    settings = SimpleNamespace(queue_download=key, queue_upload=f"{key}:processing")
    initial = await sample(pool, redis, settings, None)
    manifest = tmp_path / "cohort.json"
    write_json(manifest, initial)
    original = manifest.read_bytes()
    env = dict(os.environ, PIXAV_PHASE0_EVENTS_DIR=str(tmp_path / "events"))
    code = (
        "import time,uuid; from pixav.shared.phase0_timing import phase0_span; "
        f"span=phase0_span(uuid.UUID('{task}'),uuid.UUID('{video}'),'download'); "
        "span.__enter__(); print('ready',flush=True); time.sleep(60)"
    )
    process = subprocess.Popen(  # noqa: S603 -- fixed local drill child
        [sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout.readline().strip() == "ready"
    finally:
        process.kill()
        process.wait(timeout=10)
        process.stdout.close()
    await pool.execute("INSERT INTO tasks(video_id) VALUES($1)", video)
    # Verify project labels before the isolated restart, never infer from a port.
    info = json.loads(subprocess.check_output(["docker", "inspect", "pixav-integration-redis-1"]))[0]  # noqa: S603,S607
    assert info["Config"]["Labels"]["com.docker.compose.project"] == "pixav-integration"
    assert [m["Name"] for m in info["Mounts"]] == ["pixav-integration_integration_redis"]
    subprocess.run(  # noqa: S603 -- fixed dedicated project
        ["docker", "compose", "-f", "docker-compose.integration.yml", "restart", "redis"],  # noqa: S607
        check=True,
        capture_output=True,
        timeout=30,
    )
    await redis.aclose()
    for attempt in range(30):
        try:
            new_run = await redis_identity(redis)
            break
        except RedisConnectionError:
            if attempt == 29:
                raise
            await asyncio.sleep(0.2)
    # The fixture and subsequent tests must guard against the new live identity.
    os.environ["PIXAV_TEST_REDIS_IDENTITY"] = new_run
    assert new_run != initial["redis_run_id"]
    later = await sample(pool, redis, settings, [task])
    events = [json.loads(p.read_text()) for p in (tmp_path / "events").glob("*.json")]
    with pytest.raises(ValueError, match="instance changed"):
        build_report(initial, [later], events, {"status": "unavailable"})
    later["redis_restart_from"] = initial["redis_run_id"]
    report = build_report(initial, [later], events, {"status": "unavailable"})
    assert manifest.read_bytes() == original
    assert report["t0"] == initial["at"]
    assert report["cohort_size"] == 1
    assert report["open_span_ids"]
    assert not report["classification_complete"]
    assert report["production_gate"] == "OPEN"
