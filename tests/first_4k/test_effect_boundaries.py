"""Exercise packaged stage callers with synthetic, failure-injectable adapters.

These cover side-effect ordering and recovery decisions, not live provider acceptance.
"""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pixav.first_4k import cli, flow, playback, prepare, upload
from pixav.pixel_injector.canary import CanaryBlockedError
from pixav.pixel_injector.maestro_parts import UserActionRequiredError
from pixav.shared.models import VideoPart


@pytest.fixture
def movie(tmp_path, monkeypatch):
    for module in (flow, prepare, upload, playback, cli):
        monkeypatch.setattr(module, "WORK", tmp_path)
    monkeypatch.setattr(upload, "require_space", lambda *_: None)
    monkeypatch.setattr(prepare, "require_space", lambda *_: None)
    monkeypatch.setattr(upload, "reconcile", AsyncMock())
    monkeypatch.setattr(upload, "read_private", lambda _: {"email": "fixture@example.test", "password": "fixture"})
    client = SimpleNamespace(images=SimpleNamespace(get=lambda _: SimpleNamespace(id="fixture-image")))
    args = SimpleNamespace(
        max_parts=0,
        google_secret=tmp_path / "synthetic.json",
        max_movie_gib=1,
        min_movie_seconds=1,
        board="synthetic",
        command="run",
    )
    state = {
        "id": str(uuid.uuid4()),
        "video_id": str(uuid.uuid4()),
        "account_id": str(uuid.uuid4()),
        "stage": "prepared",
        "discovery_completed_at": "synthetic",
        "gates": {},
    }
    item = flow.MovieFlow(client, AsyncMock(), args, state)
    item.save = AsyncMock()
    item.parts = SimpleNamespace(
        list=AsyncMock(),
        journal=AsyncMock(),
        confirm_backup=AsyncMock(),
        install=AsyncMock(),
        confirm_original=AsyncMock(),
        publish=AsyncMock(),
    )
    return item


def segments(movie, count=2):
    video_id = uuid.UUID(movie.state["video_id"])
    return [
        VideoPart(
            video_id=video_id,
            part_index=i,
            manifest_version=1,
            start_seconds=i * 10,
            end_seconds=(i + 1) * 10,
            size_bytes=8,
            sha256=hashlib.sha256(b"original").hexdigest(),
            filename=f"pixav-{video_id}-part-{i:06d}-{hashlib.sha256(b'original').hexdigest()[:16]}.mp4",
        )
        for i in range(count)
    ]


@pytest.mark.parametrize(
    "case", ["complete", "pause", "challenge", "unknown", "bad-share", "quota", "no-account", "wrong-account"]
)
async def test_upload_preserves_intent_and_stops_at_failure_boundary(movie, monkeypatch, case):
    clock = datetime.now(timezone.utc)
    account_id = uuid.UUID(movie.state["account_id"])
    movie.parts.list.return_value = segments(movie)
    movie.pool.fetchrow.return_value = {
        "id": account_id,
        "email": "fixture@example.test",
        "quota_reset_at": clock + timedelta(days=1),
        "db_now": clock,
        "daily_uploaded_bytes": 0,
        "daily_quota_bytes": 1 if case == "quota" else 1024,
    }
    movie.pool.fetchval.return_value = clock + timedelta(days=1)
    scheduler = SimpleNamespace(
        next_account=AsyncMock(return_value=str(account_id)), release_lease=AsyncMock(), mark_used=AsyncMock()
    )
    if case == "no-account":
        scheduler.next_account.side_effect = RuntimeError("no quota")
    if case == "wrong-account":
        scheduler.next_account.return_value = str(uuid.uuid4())
    monkeypatch.setattr(upload, "LruAccountScheduler", lambda *_a, **_k: scheduler)
    movie.runtime = AsyncMock(return_value=(SimpleNamespace(id="guest"), object()))
    uploader = SimpleNamespace(
        login=AsyncMock(),
        push_file=AsyncMock(return_value="remote-file"),
        trigger_upload=AsyncMock(),
        backup_evidence={"backed_up": True},
    )
    verifier = SimpleNamespace(
        wait_for_share_url=AsyncMock(return_value="https://photos.google.com/synthetic"),
        validate_share_url=AsyncMock(return_value=case != "bad-share"),
    )
    monkeypatch.setattr(upload, "MaestroPartUploader", lambda *_a, **_k: uploader)
    monkeypatch.setattr(upload, "MaestroPartVerifier", lambda _: verifier)
    if case in {"challenge", "unknown"}:
        uploader.login.side_effect = UserActionRequiredError("challenge") if case == "challenge" else OSError("unknown")
    if case == "pause":
        movie.args.max_parts = 1
    if case in {"challenge", "unknown", "bad-share", "wrong-account"}:
        with pytest.raises((UserActionRequiredError, OSError, CanaryBlockedError)):
            await movie.upload()
        movie.parts.confirm_backup.assert_not_awaited()
        scheduler.mark_used.assert_not_awaited()
    else:
        await movie.upload()
        expected = {"complete": "uploaded", "pause": "upload_paused", "quota": "quota_wait", "no-account": "quota_wait"}
        assert movie.state["stage"] == expected[case]
        assert movie.parts.confirm_backup.await_count == (2 if case == "complete" else 1 if case == "pause" else 0)
    if case == "challenge":
        assert movie.state["stage"] == "user_action_required"
    if case in {"unknown", "bad-share"}:
        assert movie.state["stage"] == "reconcile"


async def test_confirmed_uploads_are_not_repeated(movie, monkeypatch):
    stamp = datetime.now(timezone.utc)
    movie.parts.list.return_value = [p.model_copy(update={"usage_counted_at": stamp}) for p in segments(movie)]
    movie.pool.fetchval.return_value = stamp
    movie.runtime = AsyncMock()
    await movie.upload()
    movie.runtime.assert_not_awaited()
    movie.parts.confirm_backup.assert_not_awaited()
    assert movie.state["stage"] == "uploaded"


@pytest.mark.parametrize("failed", [False, True])
async def test_cold_playback_never_publishes_after_failed_transfer(movie, tmp_path, monkeypatch, failed):
    stamp = datetime.now(timezone.utc)
    parts = [
        p.model_copy(update={"usage_counted_at": stamp, "share_url": "https://photos.google.com/synthetic"})
        for p in segments(movie)
    ]
    movie.parts.list.return_value = parts
    movie.state["source_provenance"] = {"reference": {"duration": 20}}
    (tmp_path / "playback").mkdir()
    report = {"parts": [{"synthetic": True}, {"synthetic": True}], "filename": "movie.mp4"}
    monkeypatch.setattr(
        playback, "monitored_run", lambda *_a, **_k: SimpleNamespace(returncode=int(failed), stdout=json.dumps(report))
    )
    if failed:
        with pytest.raises(CanaryBlockedError, match="no playback publication"):
            await movie.playback()
        movie.parts.publish.assert_not_awaited()
        assert not list((tmp_path / "playback").glob("*.strm"))
    else:
        await movie.playback()
        assert movie.state["stage"] == "playback_ready"
        assert movie.parts.confirm_original.await_count == 2
        assert (tmp_path / "playback" / f"{movie.state['video_id']}.strm").is_file()


async def test_package_candidate_commits_verified_manifest_after_source_checkpoint(movie, tmp_path, monkeypatch):
    identifier = "a" * 40
    source = tmp_path / "downloads" / identifier / "synthetic.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic media bytes")
    candidate = {
        "info_hash": identifier,
        "state": "download_complete",
        "intent_at": datetime.now(timezone.utc).isoformat(),
        "title": "Synthetic feature",
        "magnet_uri": "magnet:?xt=urn:btih:" + identifier,
    }
    torrent = AsyncMock()
    torrent.__aenter__.return_value = torrent
    torrent.has_torrent.return_value = True
    torrent.wait_complete.return_value = str(source)
    response = Mock()
    response.json.return_value = [
        {"hash": identifier, "progress": 1, "amount_left": 0, "total_size": len(source.read_bytes())}
    ]
    torrent._request.return_value = response
    monkeypatch.setattr(prepare, "MovieTorrent", lambda *_a, **_k: torrent)
    repository = SimpleNamespace(find_by_id=AsyncMock(return_value=None), insert=AsyncMock())
    monkeypatch.setattr(prepare, "VideoRepository", lambda _: repository)
    provenance = {"size": source.stat().st_size, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    checkpoint = {"synthetic": "validated source"}

    def split(path, output, video_id, **kwargs):
        kwargs["save_source_checkpoint"](checkpoint)
        assert movie.state["source_validation"] == checkpoint
        return segments(movie), dict(provenance)

    movie.media = SimpleNamespace(validate_source=lambda _: {"format": {"duration": 20}}, prepare=split)
    await movie._candidate(candidate, {"username": "fixture", "password": "fixture"})
    assert movie.state["stage"] == "prepared"
    assert movie.parts.install.await_count == 1
    assert movie.state["source_provenance"]["discovery"]["info_hash"] == identifier
    torrent.add_magnet.assert_not_awaited()
    assert source.read_bytes() == b"synthetic media bytes"


@pytest.mark.parametrize("case", ["new", "ambiguous", "unresolved"])
async def test_runtime_creation_intent_is_saved_before_external_effect(movie, monkeypatch, case):
    profile = SimpleNamespace(
        image="fixture-guest", args=[], readiness=[SimpleNamespace(command="check", contains="ready")]
    )
    monkeypatch.setattr(flow, "get_profile", lambda *_a, **_k: profile)
    monkeypatch.setattr(flow, "retained", AsyncMock())
    created = []

    def launch(*_a, **kwargs):
        role = kwargs["labels"]["pixav.photos_canary.role"]
        assert movie.state["runtime"][role + "_intent"]
        assert movie.save.await_count > 0
        created.append(role)
        return SimpleNamespace(
            id=role,
            status="running",
            exec_run=lambda args: SimpleNamespace(exit_code=0, output=b"1" if args[0] == "getprop" else b"ready"),
        )

    existing = []
    if case == "ambiguous":
        existing = [SimpleNamespace(labels={"pixav.photos_canary.role": "guest"}) for _ in range(2)]
    if case == "unresolved":
        movie.state["runtime"] = {"guest_intent": "saved"}
    movie.client.containers = SimpleNamespace(list=lambda **_: existing, run=launch)
    if case == "new":
        guest, tools = await movie.runtime()
        assert (guest.id, tools.id) == ("guest", "tools")
        assert created == ["guest", "tools"]
    else:
        with pytest.raises(CanaryBlockedError):
            await movie.runtime()
        assert created == []


@pytest.mark.parametrize(
    "stage,command,expected",
    [
        ("quota_wait", "run", "QUOTA_WAIT"),
        ("upload_paused", "run", "PART_LIMIT_REACHED"),
        ("uploaded", "run", "synthetic-verified"),
        ("prepared", "prepare-playback", "synthetic-verified"),
    ],
)
async def test_cli_dispatch_respects_wait_and_stage_boundaries(movie, monkeypatch, stage, command, expected):
    movie.state["stage"] = stage
    movie.args.command = command
    movie.state["next_action_at"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    movie.pool.fetchval.return_value = datetime.now(timezone.utc)
    for name in ("discover", "download_prepare", "upload", "playback"):
        setattr(movie, name, AsyncMock())
    movie.verify = AsyncMock(return_value={"status": "synthetic-verified"})
    launch = AsyncMock(return_value=SimpleNamespace(wait=AsyncMock(return_value=0)))
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", launch)
    result = await cli.dispatch(movie)
    assert result["status"] == expected
    if expected in {"QUOTA_WAIT", "PART_LIMIT_REACHED"}:
        movie.playback.assert_not_awaited()
        launch.assert_not_awaited()
    else:
        movie.playback.assert_awaited_once()
        movie.verify.assert_awaited_once()
