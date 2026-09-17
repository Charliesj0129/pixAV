from __future__ import annotations

import json
import stat
import uuid
from pathlib import Path
from unittest.mock import ANY, AsyncMock, patch

import pytest

from pixav.config import Settings
from scripts.instance_guard import write_backup_metadata
from scripts.phase0_backlog import (
    _assert_queue_target,
    _effective_queue_head,
    _media_evidence,
    _new_evidence_prefix,
    _qbit_cleanup_evidence,
    _row_evidence,
    _run_one,
    _sanitize_queue_payload,
    _upload_one,
    _validated_backup,
)


def _row(
    task_id: uuid.UUID,
    video_id: uuid.UUID,
    *,
    task_state: str,
    video_status: str,
    queue_name: str,
    local_path: str | None = None,
    share_url: str | None = None,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "video_id": video_id,
        "task_state": task_state,
        "video_status": video_status,
        "info_hash": "08ada5a7a6183aae1e09d831df6748d566095a10",
        "queue_name": queue_name,
        "account_id": None,
        "retries": 0,
        "max_retries": 6,
        "retry_not_before": None,
        "error_message": None,
        "local_path": local_path,
        "share_url": share_url,
        "metadata_json": {"media": {"size_bytes": 10}},
    }


def _payload(task_id: uuid.UUID, video_id: uuid.UUID) -> str:
    return json.dumps({"task_id": str(task_id), "video_id": str(video_id)})


async def test_run_one_requires_apply() -> None:
    with pytest.raises(RuntimeError, match="--apply"):
        await _run_one(apply=False, expected_db_identity="cluster")


async def test_run_one_requires_database_identity() -> None:
    with pytest.raises(RuntimeError, match="--expect-db-identity"):
        await _run_one(apply=True, expected_db_identity="")


async def test_run_one_requires_exact_task_and_video_ids() -> None:
    with pytest.raises(RuntimeError, match="--expect-task-id"):
        await _run_one(apply=True, expected_db_identity="cluster")

    with pytest.raises(RuntimeError, match="--expect-video-id"):
        await _run_one(
            apply=True,
            expected_db_identity="cluster",
            expected_task_id=str(uuid.uuid4()),
        )


async def test_run_one_refuses_verify_mode_before_connecting() -> None:
    with (
        patch("scripts.phase0_backlog.get_settings", return_value=Settings(media_loader_mode="verify")),
        patch("scripts.phase0_backlog.create_pool", new=AsyncMock()) as create_pool,
    ):
        with pytest.raises(RuntimeError, match="PIXAV_MEDIA_LOADER_MODE=full"):
            await _run_one(
                apply=True,
                expected_db_identity="cluster",
                expected_task_id=str(uuid.uuid4()),
                expected_video_id=str(uuid.uuid4()),
            )
    create_pool.assert_not_awaited()


@pytest.mark.parametrize("error", ["cookie=private", "https://private.example/share/token", "secret password failed"])
def test_row_evidence_does_not_export_unstructured_errors(error: str) -> None:
    row = _row(uuid.uuid4(), uuid.uuid4(), task_state="failed", video_status="failed", queue_name="pixav:download")
    row["error_message"] = error
    evidence = _row_evidence(row)
    assert evidence["error_class"] == "unclassified"
    assert error not in json.dumps(evidence)


async def test_effective_head_prefers_recoverable_processing_payload() -> None:
    task_id = uuid.uuid4()
    video_id = uuid.uuid4()
    redis = AsyncMock()
    redis.lindex.side_effect = [_payload(task_id, video_id)]

    head = await _effective_queue_head(redis, "pixav:download")

    assert head is not None
    assert head[1] == "processing"
    assert head[0]["task_id"] == str(task_id)
    redis.lindex.assert_awaited_once_with("pixav:download:processing", 0)


async def test_effective_head_falls_back_to_queued_payload() -> None:
    task_id = uuid.uuid4()
    video_id = uuid.uuid4()
    redis = AsyncMock()
    redis.lindex.side_effect = [None, _payload(task_id, video_id)]

    head = await _effective_queue_head(redis, "pixav:download")

    assert head is not None
    assert head[1] == "queued"


def test_queue_target_mismatch_refuses_without_dumping_unknown_fields() -> None:
    expected_task = uuid.uuid4()
    expected_video = uuid.uuid4()
    observed_task = uuid.uuid4()
    head = (
        {
            "task_id": str(observed_task),
            "video_id": str(expected_video),
            "magnet_uri": "magnet:?xt=urn:btih:secret",
        },
        "queued",
    )

    with pytest.raises(RuntimeError, match="head mismatch") as exc:
        _assert_queue_target(
            head,
            queue_name="pixav:download",
            expected_task_id=expected_task,
            expected_video_id=expected_video,
        )

    assert "magnet:" not in str(exc.value)


def test_queue_evidence_redacts_unexpected_payload_values() -> None:
    sanitized = _sanitize_queue_payload(
        {
            "task_id": "task",
            "video_id": "video",
            "magnet_uri": "magnet:?xt=urn:btih:secret",
            "cookie": "secret-cookie",
        }
    )

    assert sanitized["task_id"] == "task"
    assert sanitized["redacted_fields"] == ["cookie", "magnet_uri"]
    assert "secret-cookie" not in json.dumps(sanitized)


def test_database_backup_must_be_nonempty_owner_only_and_same_instance(tmp_path: Path) -> None:
    backup = tmp_path / "pixav.dump"
    backup.write_bytes(b"PGDMPfixture")
    backup.chmod(0o600)
    write_backup_metadata(backup, system_identifier="cluster", database="pixav")

    assert _validated_backup(backup, live_identity="cluster") == backup.resolve()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_wrong_file_with_valid_sidecar_is_not_a_full_dump(tmp_path: Path) -> None:
    backup = tmp_path / "wrong.dump"
    backup.write_bytes(b"sanitized evidence, not a dump")
    backup.chmod(0o600)
    write_backup_metadata(backup, system_identifier="cluster", database="pixav")
    with pytest.raises(RuntimeError, match="custom-format"):
        _validated_backup(backup, live_identity="cluster")


def test_preflight_and_result_names_keep_exact_task_identity(tmp_path: Path) -> None:
    task_id = uuid.uuid4()
    prefix = _new_evidence_prefix(tmp_path, command="download", task_id=task_id)
    assert str(task_id) in prefix.with_suffix(".pre.json").name
    assert str(task_id) in prefix.with_suffix(".result.json").name


def test_database_backup_rejects_group_readability(tmp_path: Path) -> None:
    backup = tmp_path / "pixav.dump"
    backup.write_bytes(b"database")
    backup.chmod(0o640)

    with pytest.raises(RuntimeError, match="0600"):
        _validated_backup(backup, live_identity="cluster")


def test_database_backup_rejects_readable_identity_sidecar(tmp_path: Path) -> None:
    backup = tmp_path / "pixav.dump"
    backup.write_bytes(b"database")
    backup.chmod(0o600)
    sidecar = write_backup_metadata(backup, system_identifier="cluster", database="pixav")
    sidecar.chmod(0o640)

    with pytest.raises(RuntimeError, match="identity sidecar.*0600"):
        _validated_backup(backup, live_identity="cluster")


def test_database_backup_rejects_symlink_after_tilde_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private_home = tmp_path / "home"
    private_home.mkdir()
    target = tmp_path / "real.dump"
    target.write_bytes(b"database")
    target.chmod(0o600)
    (private_home / "linked.dump").symlink_to(target)
    monkeypatch.setenv("HOME", str(private_home))

    with pytest.raises(RuntimeError, match="symlink"):
        _validated_backup(Path("~/linked.dump"), live_identity="cluster")


async def test_run_one_refuses_while_global_pause_is_active(tmp_path: Path) -> None:
    task_id = uuid.uuid4()
    video_id = uuid.uuid4()
    pool = AsyncMock()
    redis = AsyncMock()
    redis.info.return_value = {"run_id": "redis-run"}
    redis.get.return_value = '{"paused": true}'
    settings = Settings(vpn_egress_echo_url="")

    with (
        patch("scripts.phase0_backlog.get_settings", return_value=settings),
        patch("scripts.phase0_backlog.create_pool", new=AsyncMock(return_value=pool)),
        patch("scripts.phase0_backlog.create_redis", new=AsyncMock(return_value=redis)),
        patch("scripts.phase0_backlog.database_identity", new=AsyncMock(return_value="cluster")),
        patch("scripts.phase0_backlog.validate_selected", new=AsyncMock()),
        patch("scripts.phase0_backlog._validated_backup", return_value=tmp_path / "backup.dump"),
    ):
        with pytest.raises(RuntimeError, match="global system pause"):
            await _run_one(
                apply=True,
                expected_db_identity="cluster",
                expected_task_id=str(task_id),
                expected_video_id=str(video_id),
                database_backup=tmp_path / "backup.dump",
            )

    redis.aclose.assert_awaited_once()
    pool.close.assert_awaited_once()


@pytest.mark.parametrize("existing_local_path", [None, "/old/synthetic.mp4"])
async def test_run_one_passes_exact_guards_to_worker(tmp_path: Path, existing_local_path: str | None) -> None:
    task_id = uuid.uuid4()
    video_id = uuid.uuid4()
    settings = Settings(
        queue_download="pixav:download",
        download_dir=str(tmp_path),
        remux_dir=str(tmp_path),
        vpn_egress_echo_url="",
    )
    pre_pool = AsyncMock()
    post_pool = AsyncMock()
    pre_pool.fetchrow.return_value = _row(
        task_id,
        video_id,
        task_state="dispatched",
        video_status="discovered",
        queue_name=settings.queue_download,
        local_path=existing_local_path,
    )
    post_pool.fetchrow.return_value = _row(
        task_id,
        video_id,
        task_state="failed",
        video_status="failed",
        queue_name=settings.queue_download,
    )
    pre_redis = AsyncMock()
    pre_redis.info.return_value = {"run_id": "redis-run"}
    pre_redis.get.return_value = None
    pre_redis.lindex.side_effect = [None, _payload(task_id, video_id)]
    post_redis = AsyncMock()
    post_redis.info.return_value = {"run_id": "redis-run"}
    post_redis.lrange.side_effect = [[], []]
    run_loop = AsyncMock()

    with (
        patch("scripts.phase0_backlog.get_settings", return_value=settings),
        patch("scripts.phase0_backlog.create_pool", new=AsyncMock(side_effect=[pre_pool, post_pool])),
        patch("scripts.phase0_backlog.create_redis", new=AsyncMock(side_effect=[pre_redis, post_redis])),
        patch("scripts.phase0_backlog.database_identity", new=AsyncMock(side_effect=["cluster", "cluster"])),
        patch("scripts.phase0_backlog.validate_selected", new=AsyncMock()),
        patch("scripts.phase0_backlog._validated_backup", return_value=tmp_path / "backup.dump"),
        patch("scripts.phase0_backlog._write_preflight_evidence", new=AsyncMock()),
        patch("scripts.phase0_backlog._write_json", return_value=tmp_path / "result.json"),
        patch("scripts.phase0_backlog._media_evidence", new=AsyncMock(return_value=None)),
        patch(
            "scripts.phase0_backlog._qbit_cleanup_evidence",
            new=AsyncMock(return_value={"torrent_absent": True}),
        ),
        patch("scripts.phase0_backlog.run_loop", new=run_loop),
    ):
        if existing_local_path:
            with pytest.raises(RuntimeError, match="refuses an existing local_path"):
                await _run_one(
                    apply=True,
                    expected_db_identity="cluster",
                    expected_task_id=str(task_id),
                    expected_video_id=str(video_id),
                    database_backup=tmp_path / "backup.dump",
                    evidence_dir=tmp_path,
                )
            run_loop.assert_not_awaited()
            return
        assert (
            await _run_one(
                apply=True,
                expected_db_identity="cluster",
                expected_task_id=str(task_id),
                expected_video_id=str(video_id),
                database_backup=tmp_path / "backup.dump",
                evidence_dir=tmp_path,
            )
            == 0
        )

    assert run_loop.await_args.kwargs == {
        "max_tasks": 1,
        "expected_db_identity": "cluster",
        "expected_redis_identity": "redis-run",
        "expected_task_id": task_id,
        "expected_video_id": video_id,
    }


async def test_media_evidence_rejects_ffprobe_size_only_fallback(tmp_path: Path) -> None:
    task_id = uuid.uuid4()
    video_id = uuid.uuid4()
    media_path = tmp_path / "video.mp4"
    media_path.write_bytes(b"0" * 10)
    row = _row(
        task_id,
        video_id,
        task_state="pending",
        video_status="downloaded",
        queue_name="pixav:upload",
        local_path=str(media_path),
    )
    settings = Settings(remux_dir=str(tmp_path), vpn_egress_echo_url="")

    with patch("scripts.phase0_backlog.probe_media", new=AsyncMock(return_value={"size_bytes": 10})):
        with pytest.raises(RuntimeError, match="ffprobe did not return a readable video"):
            await _media_evidence(row, settings=settings, require_consistency=True)


async def test_qbit_cleanup_evidence_waits_for_exact_hash_and_proves_source_absent(tmp_path: Path) -> None:
    task_id = uuid.uuid4()
    video_id = uuid.uuid4()
    row = _row(
        task_id,
        video_id,
        task_state="pending",
        video_status="downloaded",
        queue_name="pixav:upload",
    )
    row["metadata_json"] = {
        "media": {"size_bytes": 10},
        "torrent": {"name": "fixture-source.mkv"},
    }
    settings = Settings(download_dir=str(tmp_path), vpn_egress_echo_url="")
    qbit = AsyncMock()
    qbit.health_check.return_value = "5.2.3"
    qbit.has_torrent.side_effect = [True, False]

    with (
        patch("scripts.phase0_backlog.QBitClient", return_value=qbit),
        patch("scripts.phase0_backlog.asyncio.sleep", new=AsyncMock()),
    ):
        evidence = await _qbit_cleanup_evidence(row, settings=settings)

    assert evidence == {
        "info_hash": "08ada5a7a6183aae1e09d831df6748d566095a10",
        "qbit_version": "5.2.3",
        "torrent_absent": True,
        "source_path_absent": True,
    }
    assert qbit.has_torrent.await_count == 2
    qbit.aclose.assert_awaited_once()


async def test_qbit_cleanup_evidence_rejects_remaining_source_path(tmp_path: Path) -> None:
    source = tmp_path / "fixture-source.mkv"
    source.write_bytes(b"leftover")
    row = _row(
        uuid.uuid4(),
        uuid.uuid4(),
        task_state="pending",
        video_status="downloaded",
        queue_name="pixav:upload",
    )
    row["metadata_json"] = {
        "media": {"size_bytes": 10},
        "torrent": {"name": source.name},
    }
    settings = Settings(download_dir=str(tmp_path), vpn_egress_echo_url="")
    qbit = AsyncMock()
    qbit.health_check.return_value = "5.2.3"
    qbit.has_torrent.return_value = False

    with patch("scripts.phase0_backlog.QBitClient", return_value=qbit):
        with pytest.raises(RuntimeError, match="source path still exists"):
            await _qbit_cleanup_evidence(row, settings=settings)

    qbit.aclose.assert_awaited_once()


async def test_upload_one_forces_local_one_shot_and_checks_postconditions(tmp_path: Path) -> None:
    task_id = uuid.uuid4()
    video_id = uuid.uuid4()
    local_path = str(tmp_path / "video.mp4")
    settings = Settings(
        queue_upload="pixav:upload",
        remux_dir=str(tmp_path),
        vpn_egress_echo_url="",
    )
    pre_pool = AsyncMock()
    post_pool = AsyncMock()
    pre_pool.fetchrow.return_value = _row(
        task_id,
        video_id,
        task_state="dispatched",
        video_status="downloaded",
        queue_name=settings.queue_upload,
        local_path=local_path,
    )
    post_pool.fetchrow.return_value = _row(
        task_id,
        video_id,
        task_state="complete",
        video_status="available",
        queue_name=settings.queue_upload,
        local_path=local_path,
        share_url=f"pixav-local://{video_id}",
    )
    pre_redis = AsyncMock()
    pre_redis.info.return_value = {"run_id": "redis-run"}
    pre_redis.get.return_value = None
    pre_redis.lindex.side_effect = [None, _payload(task_id, video_id)]
    post_redis = AsyncMock()
    post_redis.info.return_value = {"run_id": "redis-run"}
    post_redis.lrange.side_effect = [[], []]
    pixel_worker = AsyncMock()
    media_evidence = {"sizes_consistent": True}

    with (
        patch("scripts.phase0_backlog.get_settings", return_value=settings),
        patch("scripts.phase0_backlog.create_pool", new=AsyncMock(side_effect=[pre_pool, post_pool])),
        patch("scripts.phase0_backlog.create_redis", new=AsyncMock(side_effect=[pre_redis, post_redis])),
        patch("scripts.phase0_backlog.database_identity", new=AsyncMock(side_effect=["cluster", "cluster"])),
        patch("scripts.phase0_backlog.validate_selected", new=AsyncMock()),
        patch("scripts.phase0_backlog._validated_backup", return_value=tmp_path / "backup.dump"),
        patch("scripts.phase0_backlog._write_preflight_evidence", new=AsyncMock()),
        patch("scripts.phase0_backlog._write_json", return_value=tmp_path / "result.json"),
        patch("scripts.phase0_backlog._media_evidence", new=AsyncMock(return_value=media_evidence)),
        patch("scripts.phase0_backlog.run_pixel_worker", new=pixel_worker),
    ):
        assert (
            await _upload_one(
                apply=True,
                mode="local",
                expected_db_identity="cluster",
                expected_task_id=str(task_id),
                expected_video_id=str(video_id),
                database_backup=tmp_path / "backup.dump",
                evidence_dir=tmp_path,
            )
            == 0
        )

    worker_settings = pixel_worker.await_args.args[0]
    assert worker_settings.pixel_injector_mode == "local"
    assert pixel_worker.await_args.kwargs == {
        "max_tasks": 1,
        "expected_db_identity": "cluster",
        "expected_redis_identity": "redis-run",
        "expected_task_id": task_id,
        "expected_video_id": video_id,
    }


async def test_upload_one_dispatches_only_exact_pending_task_when_queue_empty(tmp_path: Path) -> None:
    task_id = uuid.uuid4()
    video_id = uuid.uuid4()
    settings = Settings(queue_upload="pixav:upload", remux_dir=str(tmp_path), vpn_egress_echo_url="")
    pre_pool = AsyncMock()
    pre_pool.fetchrow.return_value = _row(
        task_id,
        video_id,
        task_state="pending",
        video_status="downloaded",
        queue_name=settings.queue_upload,
        local_path=str(tmp_path / "video.mp4"),
    )
    redis = AsyncMock()
    redis.info.return_value = {"run_id": "redis-run"}
    redis.get.return_value = None
    redis.lindex.side_effect = [None, None, None, _payload(task_id, video_id)]
    task_repo = AsyncMock()
    task_repo.claim_for_dispatch.return_value = True
    dispatcher = AsyncMock()
    stop_after_dispatch = RuntimeError("stop after guarded dispatch")

    with (
        patch("scripts.phase0_backlog.get_settings", return_value=settings),
        patch("scripts.phase0_backlog.create_pool", new=AsyncMock(return_value=pre_pool)),
        patch("scripts.phase0_backlog.create_redis", new=AsyncMock(return_value=redis)),
        patch("scripts.phase0_backlog.database_identity", new=AsyncMock(return_value="cluster")),
        patch("scripts.phase0_backlog.validate_selected", new=AsyncMock()),
        patch("scripts.phase0_backlog._validated_backup", return_value=tmp_path / "backup.dump"),
        patch("scripts.phase0_backlog._write_preflight_evidence", new=AsyncMock()),
        patch("scripts.phase0_backlog._media_evidence", new=AsyncMock(return_value={"sizes_consistent": True})),
        patch("scripts.phase0_backlog.TaskRepository", return_value=task_repo),
        patch("scripts.phase0_backlog.RedisTaskDispatcher", return_value=dispatcher),
        patch("scripts.phase0_backlog.run_pixel_worker", new=AsyncMock(side_effect=stop_after_dispatch)),
    ):
        with pytest.raises(RuntimeError, match="stop after guarded dispatch"):
            await _upload_one(
                apply=True,
                mode="local",
                expected_db_identity="cluster",
                expected_task_id=str(task_id),
                expected_video_id=str(video_id),
                database_backup=tmp_path / "backup.dump",
                evidence_dir=tmp_path,
            )

    task_repo.claim_for_dispatch.assert_awaited_once_with(task_id, next_state=ANY)
    dispatcher.dispatch.assert_awaited_once_with(str(task_id), settings.queue_upload)
