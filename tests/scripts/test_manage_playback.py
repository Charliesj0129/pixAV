"""Operator surface for playback readiness and stream verification (BDD-058, 068, 069)."""

from __future__ import annotations

import argparse
import uuid

import pytest

import scripts.manage_playback as cli
from pixav.shared.playback_models import PlayableAsset

IDENTITY = "7682619096198328359"
VIDEO = uuid.UUID("77777777-7777-4777-8777-777777777777")
DIGEST = "a" * 64


class Pool:
    def __init__(self, identity: str = IDENTITY, rows=()) -> None:
        self.identity, self.rows, self.closed = identity, rows, False

    async def fetchval(self, query, *args):
        assert "pg_control_system" in query
        return self.identity

    async def fetch(self, query, *args):
        return list(self.rows)

    async def close(self):
        self.closed = True


class Repository:
    def __init__(self, asset: PlayableAsset | None, *, accepts: bool = True) -> None:
        self.asset, self.accepts = asset, accepts
        self.recorded: list = []

    async def eligible_for_video(self, video_id):
        return self.asset

    async def record_playback_verification(self, playable_asset_id, evidence):
        self.recorded.append((playable_asset_id, evidence))
        return self.accepts


def ready_asset() -> PlayableAsset:
    return PlayableAsset(video_id=VIDEO, remote_asset_id=uuid.uuid4(), state="READY", size_bytes=2048, sha256=DIGEST)


@pytest.fixture()
def wire(monkeypatch):
    def install(pool: Pool, repository: Repository | None = None, acceptance=None):
        async def factory(*_args, **_kwargs):
            return pool

        monkeypatch.setattr(cli, "get_settings", lambda: object())
        monkeypatch.setattr(cli, "create_pool", factory)
        monkeypatch.setattr(cli, "PlayableAssetRepository", lambda _pool: repository)
        if acceptance is not None:
            monkeypatch.setattr(cli, "stable_acceptance", acceptance)
        return pool

    return install


def args(command: str, **changes) -> argparse.Namespace:
    fields = {
        "command": command,
        "db_identity": IDENTITY,
        "video_id": VIDEO,
        "limit": 50,
        "base_url": "http://resolver:8000/",
        "operator": "operator",
        "reason": "post-cutover check",
    }
    return argparse.Namespace(**{**fields, **changes})


async def passing(base, video_id, size, sha256):
    assert (base, video_id, size, sha256) == ("http://resolver:8000", str(VIDEO), 2048, DIGEST)
    return {"head": "PASS", "get": "PASS", "range": "PASS", "suffix": "PASS", "416": "PASS", "sha256": sha256}


async def test_verify_records_evidence_only_after_every_check_passes_bdd_069(wire, capsys):
    repository = Repository(ready_asset())
    wire(Pool(), repository, passing)

    assert await cli._run(args("verify")) == 0

    [(playable_id, evidence)] = repository.recorded
    assert playable_id == repository.asset.id
    assert evidence["method"] == cli.VERIFICATION_METHOD and evidence["get"] == "PASS"
    assert (evidence["operator"], evidence["reason"]) == ("operator", "post-cutover check")
    assert IDENTITY in capsys.readouterr().out


async def test_failed_acceptance_records_nothing(wire, capsys):
    async def failing(*_args):
        raise ValueError("HTTP complete GET contract failed (503)")

    repository = Repository(ready_asset())
    wire(Pool(), repository, failing)

    assert await cli._run(args("verify")) == 1
    assert repository.recorded == []
    assert "nothing recorded" in capsys.readouterr().out


async def test_a_video_without_a_ready_object_is_refused(wire, capsys):
    repository = Repository(None)
    wire(Pool(), repository, passing)

    assert await cli._run(args("verify")) == 2
    assert repository.recorded == [] and "no READY" in capsys.readouterr().out


async def test_an_object_that_changed_meanwhile_is_reported(wire, capsys):
    repository = Repository(ready_asset(), accepts=False)
    wire(Pool(), repository, passing)

    assert await cli._run(args("verify")) == 2
    assert "changed" in capsys.readouterr().out


async def test_verification_refuses_a_different_cluster(wire, capsys):
    repository = Repository(ready_asset())
    wire(Pool(identity="1111"), repository, passing)

    assert await cli._run(args("verify", db_identity="2222")) == 2
    assert repository.recorded == [] and "refusing" in capsys.readouterr().out


async def test_status_prints_facts_and_no_location(wire, capsys):
    row = {"id": uuid.uuid4(), "video_id": VIDEO, "state": "READY", "sha256": DIGEST, "playback_verified_at": None}
    pool = wire(Pool(rows=[row]))

    assert await cli._run(args("status")) == 0

    printed = capsys.readouterr().out
    assert "READY" in printed and IDENTITY in printed
    for forbidden in ("share_url", "photos.app.goo.gl", "/data/", "evidence"):
        assert forbidden not in printed
    assert pool.closed


async def test_verification_requires_an_explicit_cluster(wire):
    repository = Repository(ready_asset())
    wire(Pool(), repository, passing)
    assert await cli._run(args("verify", db_identity=None)) == 2
    assert repository.recorded == []


async def test_client_report_without_recording_is_refused(wire, tmp_path):
    repository = Repository(ready_asset())
    wire(Pool(), repository, passing)
    assert await cli._run(args("verify", client_evidence=tmp_path / "report.json")) == 2
    assert repository.recorded == []


async def test_client_attestation_is_bound_to_object_and_private_recording(wire, tmp_path):
    import hashlib
    import json
    from datetime import datetime, timezone

    repository = Repository(ready_asset())
    asset = repository.asset
    wire(Pool(), repository, passing)
    recording = tmp_path / "synthetic-recording.bin"
    recording.write_bytes(b"synthetic test evidence; not live acceptance")
    report = {
        "method": "jellyfin-client-v1",
        "video_id": str(VIDEO),
        "playable_asset_id": str(asset.id),
        "remote_asset_id": str(asset.remote_asset_id),
        "preparation_version": asset.preparation_version,
        "sha256": DIGEST,
        "size": 2048,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "client_version": "synthetic-client",
        "server_version": "synthetic-server",
        "operator": "operator",
        "recording_sha256": hashlib.sha256(recording.read_bytes()).hexdigest(),
        "beginning_audio_video": "PASS",
        "middle_audio_video": "PASS",
        "end_audio_video": "PASS",
        "seek": "PASS",
        "resume": "PASS",
    }
    evidence = tmp_path / "client.json"
    evidence.write_text(json.dumps(report))
    assert await cli._run(args("verify", client_evidence=evidence, recording=recording)) == 0
    assert repository.recorded[0][1]["client_playback"]["recording_sha256"] == report["recording_sha256"]
    repository.recorded.clear()
    recording.write_bytes(b"different recording")
    assert await cli._run(args("verify", client_evidence=evidence, recording=recording)) == 2
    assert repository.recorded == []
    recording.write_bytes(b"synthetic test evidence; not live acceptance")
    report["video_id"] = str(uuid.uuid4())
    evidence.write_text(json.dumps(report))
    assert await cli._run(args("verify", client_evidence=evidence, recording=recording)) == 2
    assert repository.recorded == []
