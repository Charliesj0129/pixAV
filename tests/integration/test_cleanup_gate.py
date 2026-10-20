"""Real PostgreSQL cleanup authorization.

Staging deletion is the one irreversible step in the pipeline, so these tests
are written from the refusals inward: every guarantee is removed in turn and the
artifact must survive. Publication, playable objects and stream checks are real
rows, written the way LibraryProjection and PlaybackResolver write them; only
the last tests hold every guarantee at once, and only then is a file removed.
"""

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest

from pixav.library_projection.artwork import ArtworkStore
from pixav.library_projection.metadata import EffectiveMetadata
from pixav.library_projection.render import render_projection
from pixav.library_projection.repository import ArtworkRecord
from pixav.library_projection.writer import ProjectionWriter
from pixav.maxwell_core.cleanup_authorization import CleanupAuthorizer
from pixav.maxwell_core.gc import LocalFileJanitor
from pixav.shared.cleanup_binding import capture_cleanup_binding
from pixav.shared.cleanup_recovery import (
    CleanupApprovalError,
    CleanupApprovalRequest,
    CleanupRecoveryAuthorization,
    CleanupRuntimeBinding,
    target_binding_digest,
)
from pixav.shared.media_file import open_media_file
from pixav.shared.playable_assets import PlayableAssetRepository
from pixav.shared.remote_assets import RemoteAssetRepository

pytestmark = pytest.mark.integration

# Policy fixtures only; these values do not attest a live daemon or restore.
RUNTIME = CleanupRuntimeBinding(docker_context="synthetic", docker_daemon_id="synthetic-daemon", redis_run_id="a" * 40)


@pytest.fixture
async def db(integration_db):
    for path in sorted(Path("migrations").glob("*.sql")):
        await integration_db.execute(path.read_text())
    return integration_db


async def artifact(db, tmp_path, *, durable=True, due=True):
    """A video whose staging file is a candidate for removal."""
    media = tmp_path / "prepared.mp4"
    media.write_bytes(b"the only local copy")
    video = await db.fetchval(
        """INSERT INTO videos(title,local_path,local_cleanup_after)
        VALUES('synthetic fixture',$1, CASE WHEN $2 THEN now()-interval '1 day' ELSE now()+interval '1 day' END)
        RETURNING id""",
        str(media),
        due,
    )
    if durable:
        task = uuid.uuid4()
        await db.execute(
            "INSERT INTO tasks(id,video_id,queue_name,state) VALUES($1,$2,'pixav:media-managed','complete')",
            task,
            video,
        )
        await db.execute("INSERT INTO workflow_tasks(task_id,video_id) VALUES($1,$2)", task, video)
        execution = uuid.uuid4()
        await db.execute(
            "INSERT INTO executions(id,task_id,max_retries,state,stage) VALUES($1,$2,1,'SUCCEEDED','verify')",
            execution,
            task,
        )
        operation, artifact_id = uuid.uuid4(), uuid.uuid4()
        await db.execute(
            "INSERT INTO operation_intents(id,execution_id,stage,identity) VALUES($1,$2,'prepare','synthetic')",
            operation,
            execution,
        )
        await db.execute(
            "INSERT INTO workflow_artifacts(id,execution_id,operation_id,path,facts) VALUES($1,$2,$3,$4,'{}')",
            artifact_id,
            execution,
            operation,
            str(media),
        )
        await db.execute(
            """INSERT INTO remote_assets(id,video_id,artifact_id,policy_version,expected,segment_count,
            state,durable_at) VALUES($1,$2,$3,'photos-original-v1','{}',1,'DURABLE',now())""",
            uuid.uuid4(),
            video,
            artifact_id,
        )
    return video, media


async def publish(db, video):
    """A complete projection of the video's READY object, as LibraryProjection records it.

    A video with no remote copy has nothing that could be READY, so nothing is
    published for it and the refusal names every guarantee that is missing.
    """
    playable = await playback_ready(db, video, verified=False)
    if playable is None:
        return
    root = Path(await db.fetchval("SELECT local_path FROM videos WHERE id=$1", video)).parent / "library"
    store = ArtworkStore(root / "artwork")
    root.mkdir(parents=True, exist_ok=True)
    poster = root / "fixture.png"
    poster.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
    sha, size, mime = store.import_file(poster)
    artwork = ArtworkRecord(
        id=uuid.uuid4(), video_id=video, kind="poster", sha256=sha, size_bytes=size, media_type=mime
    )
    spec = render_projection(
        video, EffectiveMetadata(title="Synthetic"), [artwork], resolver_base_url="http://resolver"
    )
    digest = spec.digest
    ProjectionWriter(root, store).publish(spec, 1)
    await db.execute(
        """INSERT INTO library_publications(video_id,playable_asset_id,state,layout_version,projection_version,
        desired_digest,active_digest,active_version,published_at)
        VALUES($1,$2,'PUBLISHED','jellyfin-movie-v1',1,$3,$3,$4,now())""",
        video,
        playable,
        digest,
        f"1-{digest[:12]}",
    )


def janitor(db, tmp_path, *, apply=True):
    return LocalFileJanitor(
        db,
        download_dir=str(tmp_path),
        library_root=str(tmp_path / "library"),
        apply_deletions=apply,
        recovery_runtime=RUNTIME,
        allow_operator_exception=True,
    )


async def test_projection_absent_always_rejects_bdd_005_131(db, tmp_path):
    """Without a projection or a playable object, nothing is cleanup-eligible."""
    _, media = await artifact(db, tmp_path)

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.read_bytes() == b"the only local copy"
    decision = await db.fetchrow("SELECT * FROM cleanup_audit")
    assert decision["decision"] == "rejected" and decision["applied"] is False


async def test_no_durable_remote_asset_rejects_bdd_004_130(db, tmp_path):
    video, media = await artifact(db, tmp_path, durable=False)
    await publish(db, video)

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.exists()
    reasons = await db.fetchval("SELECT reasons FROM cleanup_audit")
    assert '"durable_remote_asset": false' in reasons


async def test_created_but_unverified_asset_rejects_bdd_047_059(db, tmp_path):
    """An upload the provider accepted is not yet a copy anyone has read back."""
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await db.execute("UPDATE remote_assets SET state='CREATED',durable_at=NULL")

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.exists()


async def test_open_execution_rejects_bdd_132(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await db.execute("UPDATE executions SET state='RUNNING'")

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.exists()


async def test_active_reader_rejects_bdd_060(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await db.execute(
        """INSERT INTO reader_leases(id,video_id,path,holder,lease_until)
        VALUES($1,$2,$3,'resolver',now()+interval '5 minutes')""",
        uuid.uuid4(),
        video,
        str(media),
    )

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.exists()


async def test_live_filesystem_reader_blocks_cleanup_without_a_database_lease(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import fcntl,sys;f=open(sys.argv[1],'rb');fcntl.flock(f,fcntl.LOCK_SH);print('READY',flush=True);sys.stdin.read();f.close()",
        str(media),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), timeout=5) == b"READY\n"
        assert process.returncode is None
        stats = await janitor(db, tmp_path).cleanup()
        assert stats["deleted"] == 0 and stats["rejected"] == 1
        assert media.read_bytes() == b"the only local copy"
        assert await db.fetchval("SELECT count(*) FROM cleanup_journal") == 0
        assert await db.fetchval("SELECT local_path FROM videos WHERE id=$1", video) == str(media)
    finally:
        process.stdin.close()
        await asyncio.wait_for(process.wait(), timeout=5)


async def test_expired_reader_lease_no_longer_blocks_bdd_060(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await db.execute(
        """INSERT INTO reader_leases(id,video_id,path,holder,lease_until)
        VALUES($1,$2,$3,'resolver',now()-interval '1 second')""",
        uuid.uuid4(),
        video,
        str(media),
    )

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.exists()


async def test_retention_not_expired_is_not_even_considered_bdd_006(db, tmp_path):
    video, media = await artifact(db, tmp_path, due=False)
    await publish(db, video)

    stats = await janitor(db, tmp_path).cleanup()

    assert stats == {"deleted": 0, "missing": 0, "failed": 0, "unsafe": 0, "rejected": 0, "eligible": 0}
    assert media.exists()
    assert await db.fetchval("SELECT count(*) FROM cleanup_audit") == 0


async def test_dry_run_changes_nothing_bdd_133(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)

    stats = await janitor(db, tmp_path, apply=False).cleanup()

    assert stats["rejected"] == 1 and stats["deleted"] == 0
    assert media.read_bytes() == b"the only local copy"
    assert await db.fetchval("SELECT local_path FROM videos WHERE id=$1", video) == str(media)
    assert await db.fetchval("SELECT count(*) FROM cleanup_audit") == 0


async def test_eligible_dry_run_keeps_staging_metadata_and_database_unchanged(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    os.utime(media, ns=(1_000_000_000, media.stat().st_mtime_ns))
    await authorize(db, video, media)
    before = media.stat()
    names = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    snapshot_sql = """SELECT md5(jsonb_build_array(
        (SELECT jsonb_agg(to_jsonb(v) ORDER BY id) FROM videos v),
        (SELECT jsonb_agg(to_jsonb(a) ORDER BY id) FROM cleanup_authorizations a),
        (SELECT jsonb_agg(to_jsonb(j) ORDER BY id) FROM cleanup_journal j),
        (SELECT jsonb_agg(to_jsonb(a) ORDER BY id) FROM cleanup_audit a)
    )::text)"""
    database_before = await db.fetchval(snapshot_sql)

    stats = await janitor(db, tmp_path, apply=False).cleanup()

    assert stats["eligible"] == 1 and stats["deleted"] == stats["rejected"] == 0
    after = media.stat()
    assert (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_atime_ns,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (after.st_dev, after.st_ino, after.st_size, after.st_atime_ns, after.st_mtime_ns, after.st_ctime_ns)
    assert names == sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert await db.fetchval(snapshot_sql) == database_before


async def test_same_bytes_at_a_new_inode_do_not_reuse_an_earlier_authorization(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    media.rename(tmp_path / "retained-original.mp4")
    media.write_bytes(b"the only local copy")

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.read_bytes() == b"the only local copy"
    assert await db.fetchval("SELECT count(*) FROM cleanup_journal") == 0
    assert '"exact_target_backup_authorization": false' in await reasons_for(db)


@pytest.mark.parametrize("old_schema", [False, True])
@pytest.mark.parametrize("column", ["target_binding", "recovery_authorization", "approval_sha256"])
async def test_legacy_free_text_approval_cannot_authorize_cleanup(db, tmp_path, old_schema, column):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    if old_schema:
        await db.execute(f"ALTER TABLE cleanup_authorizations DROP COLUMN {column}")  # noqa: S608 -- fixed parameters
    else:
        await db.execute(f"UPDATE cleanup_authorizations SET {column}=NULL")  # noqa: S608 -- fixed parameters

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.exists()
    assert '"exact_target_backup_authorization": false' in await reasons_for(db)


@pytest.mark.parametrize(
    "field,value",
    [
        ("database_identifier", "1"),
        ("database_name", "another_database"),
        ("playable_asset_id", "11111111-1111-4111-8111-111111111111"),
        ("remote_asset_id", "22222222-2222-4222-8222-222222222222"),
        ("publication_digest", "f" * 64),
    ],
)
async def test_approval_cannot_be_transplanted_between_instances_or_assets(db, tmp_path, field, value):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    binding = json.loads(await db.fetchval("SELECT target_binding FROM cleanup_authorizations"))
    binding[field] = value
    await db.execute("UPDATE cleanup_authorizations SET target_binding=$1::jsonb", json.dumps(binding))

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.exists()
    assert '"exact_target_backup_authorization": false' in await reasons_for(db)


@pytest.mark.parametrize("change", ["preparation", "playback_verification", "publication"])
async def test_new_domain_revision_requires_new_target_approval(db, tmp_path, change):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    if change == "preparation":
        await db.execute("UPDATE playable_assets SET preparation_version='replacement-version'")
    elif change == "playback_verification":
        await db.execute("UPDATE playable_assets SET playback_verified_at=now()")
    else:
        await db.execute("UPDATE library_publications SET active_version='2-aaaaaaaaaaaa'")

    assert not await janitor(db, tmp_path)._authorized_target(db, video, str(media))
    assert media.exists()


async def test_inode_lock_remains_held_through_unlink_and_domain_commit(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    alias = tmp_path / "retained-alias.mp4"
    os.link(media, alias)
    await authorize(db, video, media)
    observed = []

    class InspectCommit(LocalFileJanitor):
        async def _audit(self, conn, video, path, decision, applied, reasons):
            if decision == "deleted":
                assert not media.exists()
                with pytest.raises(BlockingIOError), open_media_file(alias):
                    pytest.fail("reader opened the inode before the domain transaction committed")
                observed.append("locked")
            await super()._audit(conn, video, path, decision, applied, reasons)

    operator = InspectCommit(
        db,
        download_dir=str(tmp_path),
        library_root=str(tmp_path / "library"),
        recovery_runtime=RUNTIME,
        allow_operator_exception=True,
    )
    assert (await operator.cleanup(apply=True))["deleted"] == 1
    assert observed == ["locked"]
    with open_media_file(alias) as reader:
        assert reader.facts()[1] == 19
    assert await db.fetchval("SELECT local_path FROM videos WHERE id=$1", video) is None


async def test_binding_keeps_the_configured_staging_root_symlink_contract(db, tmp_path):
    root = tmp_path / "actual-staging"
    root.mkdir()
    video, media = await artifact(db, root)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    configured = tmp_path / "configured-staging"
    configured.symlink_to(root, target_is_directory=True)
    domain_path = str(configured / media.name)
    await db.execute("UPDATE videos SET local_path=$1 WHERE id=$2", domain_path, video)
    await db.execute("UPDATE cleanup_authorizations SET path=$1 WHERE video_id=$2", domain_path, video)

    operator = LocalFileJanitor(
        db,
        download_dir=str(configured),
        library_root=str(root / "library"),
        recovery_runtime=RUNTIME,
        allow_operator_exception=True,
    )

    assert (await operator.cleanup(apply=True))["deleted"] == 1
    assert not media.exists()


async def test_publication_row_cannot_substitute_for_playback_evidence_bdd_005_131(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    neighbour = tmp_path / "unrelated.mp4"
    neighbour.write_bytes(b"not approved")

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.exists()
    assert neighbour.read_bytes() == b"not approved"
    decision = await db.fetchrow("SELECT * FROM cleanup_audit")
    assert decision["decision"] == "rejected" and decision["applied"] is False
    assert await db.fetchval("SELECT local_path FROM videos WHERE id=$1", video) == str(media)
    assert await db.fetchval("SELECT state FROM remote_assets WHERE video_id=$1", video) == "DURABLE"


async def test_target_outside_the_configured_root_is_never_unlinked_bdd_061(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    root = tmp_path / "staging"
    root.mkdir()

    stats = await janitor(db, root).cleanup()

    assert stats["unsafe"] == 1 and stats["deleted"] == 0
    assert media.exists()
    assert await db.fetchval("SELECT decision FROM cleanup_audit") == "unsafe"


async def test_symlink_escape_is_never_followed_bdd_062(db, tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"not ours")
    link = root / "prepared.mp4"
    link.symlink_to(outside)
    video = await db.fetchval(
        """INSERT INTO videos(title,local_path,local_cleanup_after)
        VALUES('synthetic fixture',$1,now()-interval '1 day') RETURNING id""",
        str(link),
    )
    await publish(db, video)

    stats = await janitor(db, root).cleanup()

    assert stats["unsafe"] == 1 and stats["deleted"] == 0
    assert outside.read_bytes() == b"not ours"
    assert link.is_symlink()


async def test_missing_file_does_not_prove_cleanup_was_authorized(db, tmp_path):
    """Missing staging cannot manufacture integrity or playback evidence."""
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    media.unlink()

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["rejected"] == 1 and stats["deleted"] == 0
    assert await db.fetchval("SELECT local_path FROM videos WHERE id=$1", video) == str(media)
    assert await db.fetchval("SELECT decision FROM cleanup_audit") == "rejected"


async def test_durable_fact_survives_a_crash_before_cleanup_bdd_114(db, tmp_path):
    """Verification already happened; a crash on the way to cleanup changes nothing."""
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    durable_at = await db.fetchval("SELECT durable_at FROM remote_assets WHERE video_id=$1", video)

    # The process died here. Nothing was written between verification and now.
    first = await janitor(db, tmp_path).cleanup()
    second = await janitor(db, tmp_path).cleanup()

    assert first["rejected"] == 1 and second["rejected"] == 1
    asset = await db.fetchrow("SELECT * FROM remote_assets WHERE video_id=$1", video)
    assert asset["state"] == "DURABLE" and asset["durable_at"] == durable_at
    assert media.read_bytes() == b"the only local copy"
    assert await db.fetchval("SELECT count(*) FROM cleanup_audit") == 2, "each evaluation is audited on its own"


async def test_eligibility_is_recomputed_rather_than_remembered_bdd_114(db, tmp_path):
    """A guarantee that disappears after the first evaluation must block the next."""
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await janitor(db, tmp_path).cleanup()

    # A new execution takes an interest in the same media after the crash.
    task = await db.fetchval("SELECT task_id FROM workflow_tasks WHERE video_id=$1", video)
    await db.execute(
        "INSERT INTO executions(id,task_id,max_retries,state,stage) VALUES($1,$2,1,'READY','verify')",
        uuid.uuid4(),
        task,
    )
    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0
    assert media.exists()
    reasons = await db.fetchval("SELECT reasons FROM cleanup_audit ORDER BY created_at DESC LIMIT 1")
    assert "no_open_execution" in reasons


# ── The two named producers cleanup is waiting on ───────────────────────────


async def playback_ready(db, video, *, verified):
    """The video's READY playable object on its remote copy, stream-checked or not."""
    remote = await db.fetchval("SELECT id FROM remote_assets WHERE video_id=$1", video)
    if remote is None:
        return None
    playable = await db.fetchval("SELECT id FROM playable_assets WHERE video_id=$1 AND state='READY'", video)
    if playable is None:
        playable = uuid.uuid4()
        await db.execute(
            """INSERT INTO playable_assets(id,video_id,remote_asset_id,preparation_version,state,size_bytes,sha256,
            ready_at,materialized_at) VALUES($1,$2,$3,'photos-playback-v1','READY',19,$4,
            now()-interval '1 hour',now()-interval '1 hour')""",
            playable,
            video,
            remote,
            "d" * 64,
        )
    if verified:
        # These fixtures model already completed playback. Keep them in explicit
        # history so small host clock corrections cannot create future evidence.
        # Separate tests still verify future and before-READY evidence rejection.
        observed = await db.fetchval("SELECT now()-interval '30 minutes'")
        client = {
            "method": "jellyfin-client-v1",
            "video_id": str(video),
            "playable_asset_id": str(playable),
            "remote_asset_id": str(remote),
            "preparation_version": "photos-playback-v1",
            "sha256": "d" * 64,
            "size": 19,
            "observed_at": observed.isoformat(),
            "client_version": "synthetic-client",
            "server_version": "synthetic-server",
            "operator": "test",
            "recording_sha256": "e" * 64,
            "beginning_audio_video": "PASS",
            "middle_audio_video": "PASS",
            "end_audio_video": "PASS",
            "seek": "PASS",
            "resume": "PASS",
        }
        await db.execute(
            "UPDATE playable_assets SET playback_verified_at=$3,playback_evidence=$2::jsonb WHERE id=$1",
            playable,
            json.dumps({"client_playback": client}),
            observed,
        )
    return playable


@pytest.mark.parametrize("mismatch", [None, "playable_asset_id", "remote_asset_id", "sha256"])
async def test_http_reverification_preserves_only_matching_client_evidence(db, tmp_path, mismatch):
    video, _ = await artifact(db, tmp_path)
    playable = await playback_ready(db, video, verified=True)
    previous = json.loads(await db.fetchval("SELECT playback_evidence FROM playable_assets WHERE id=$1", playable))
    if mismatch:
        previous["client_playback"][mismatch] = "f" * 64 if mismatch == "sha256" else str(uuid.uuid4())
        await db.execute(
            "UPDATE playable_assets SET playback_evidence=$2::jsonb WHERE id=$1", playable, json.dumps(previous)
        )
    http = {"method": "stable-http-v1", "size": 19, "sha256": "d" * 64, "get": "PASS"}
    assert await PlayableAssetRepository(db).record_playback_verification(playable, http)
    stored = json.loads(await db.fetchval("SELECT playback_evidence FROM playable_assets WHERE id=$1", playable))
    assert stored["get"] == "PASS"
    assert ("client_playback" in stored) == (mismatch is None)
    if mismatch is None:
        assert stored["client_playback"] == previous["client_playback"]
    assert "client_playback" not in http, "the caller's report must remain unchanged"


async def test_v2_review_registered_and_cleanup_rechecks_manifest_and_reference_bdd_135_143(db, tmp_path):
    from pixav.shared.client_playback_evidence import playback_manifest_digest

    video, media = await artifact(db, tmp_path)
    playable = await playback_ready(db, video, verified=True)
    previous = json.loads(await db.fetchval("SELECT playback_evidence FROM playable_assets WHERE id=$1", playable))
    client = previous["client_playback"]
    remote = uuid.UUID(client["remote_asset_id"])
    await db.execute("UPDATE workflow_artifacts SET facts=$1::jsonb", json.dumps({"facts": {"sha256": "c" * 64}}))
    await db.execute(
        """INSERT INTO remote_asset_segments(asset_id,segment_index,start_seconds,end_seconds,size_bytes,sha256,local_path,media_info)
        VALUES($1,0,0,60,19,$2,'synthetic','{}')""",
        remote,
        "d" * 64,
    )
    manifest = [dict(x) for x in await db.fetch("SELECT * FROM remote_asset_segments WHERE asset_id=$1", remote)]
    confirmed = await db.fetchval("SELECT now()-interval '10 minutes'")
    client.update(
        method="jellyfin-client-v2",
        manifest_sha256=playback_manifest_digest(manifest),
        reference_source_sha256="c" * 64,
        reference_sha256="f" * 64,
        trace_sha256="a" * 64,
        analysis_sha256="b" * 64,
        item_id="fixture-item",
        session_id="fixture-session",
        device_id="fixture-device",
        duration_seconds=60,
        junction_seconds=[],
        saved_resume_seconds=30,
        operator_confirmed_at=confirmed.isoformat(),
        windows=[
            {
                "kind": kind,
                "expected_seconds": target,
                "content_seconds": target,
                "recording_seconds": i * 20,
                "duration_seconds": 10,
                "uncertainty_seconds": 0.5,
                "visual_confirmation": "PASS",
                "audio_confirmation": "PASS",
                "identifiable": True,
                "continuous": True,
            }
            for i, (kind, target) in enumerate(
                (("beginning", 0), ("middle", 25), ("end", 50), ("seek", 20), ("resume", 30))
            )
        ],
    )
    repo = PlayableAssetRepository(db)
    http = {"size": 19, "sha256": "d" * 64}
    assert await repo.record_playback_verification(playable, {**http, "client_playback": client})
    gate = janitor(db, tmp_path, apply=False)
    assert await gate._verified_playback(db, video)
    client["reference_source_sha256"] = "b" * 64
    assert not await repo.record_playback_verification(playable, {**http, "client_playback": client})
    await db.execute("UPDATE remote_asset_segments SET sha256=$1 WHERE asset_id=$2", "f" * 64, remote)
    assert not await gate._verified_playback(db, video)
    assert await repo.record_playback_verification(playable, http)
    stored = json.loads(await db.fetchval("SELECT playback_evidence FROM playable_assets WHERE id=$1", playable))
    assert "client_playback" not in stored
    assert media.read_bytes() == b"the only local copy"


async def playback_verified(db, video):
    """PlaybackResolver's end-to-end stream check, so the other half can be exercised alone."""
    await playback_ready(db, video, verified=True)


async def authorize(db, video, media, **overrides):
    """Write a synthetic exception stamp to isolate the janitor's live gates.

    This helper does not test the operator/receipt boundary or claim a backup.
    """
    with open_media_file(media) as source:
        facts = source.facts()
        binding = await capture_cleanup_binding(db, video, media, source.descriptor, facts)
    now = await db.fetchval("SELECT clock_timestamp()")
    recovery = CleanupRecoveryAuthorization(
        version="cleanup-recovery-authorization-v1",
        mode="operator-exception",
        scope="isolated-acceptance",
        target_binding_sha256=target_binding_digest(binding),
        runtime=RUNTIME,
        receipt_sha256="b" * 64,
        proof_sha256="c" * 64,
        validated_at=now,
        expires_at=now + timedelta(minutes=30),
        off_host_attested=False,
    )
    fields = {
        "path": str(media),
        "artifact_sha256": facts[0],
        "size_bytes": facts[1],
        "expires": "now()+interval '1 hour'",
    }
    fields.update(overrides)
    asset = await db.fetchval("SELECT id FROM remote_assets WHERE video_id=$1", video)
    await db.execute(
        f"""INSERT INTO cleanup_authorizations(id,video_id,path,artifact_sha256,size_bytes,remote_asset_id,
        playback_verified_at,backup_reference,approved_by,reason,expires_at,target_binding,recovery_authorization,approval_sha256)
        VALUES($1,$2,$3,$4,$5,$6,now(),'synthetic-receipt','fixture-operator','integration fixture',
        {fields["expires"]},$7::jsonb,$8::jsonb,$9)""",  # noqa: S608 - interval literal comes from this module only
        uuid.uuid4(),
        video,
        fields["path"],
        fields["artifact_sha256"],
        fields["size_bytes"],
        asset,
        binding.model_dump_json(),
        recovery.model_dump_json(),
        "d" * 64,
    )


async def reasons_for(db):
    return await db.fetchval("SELECT reasons FROM cleanup_audit ORDER BY created_at DESC LIMIT 1")


async def private_approval_request(db, tmp_path, video, media, *, runtime=RUNTIME):
    """Synthetic operator attestation only; no actual backup/restore operation."""
    tmp_path.chmod(0o700)
    with open_media_file(media) as source:
        binding = await capture_cleanup_binding(db, video, media, source.descriptor, source.facts())
    now = await db.fetchval("SELECT clock_timestamp()")

    def evidence(name, payload):
        raw = json.dumps(payload).encode()
        path = tmp_path / name
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}

    instruction = evidence("instruction.json", {"instruction": "Synthetic operator explicitly waives a backup."})
    receipt = evidence(
        "receipt.json",
        {
            "kind": "operator-exception",
            "scope": "isolated-acceptance",
            "authorized_by": "fixture-operator",
            "reason": "Synthetic exception",
            "instruction": instruction,
            "runtime": runtime.model_dump(),
            "database_identifier": binding.database_identifier,
            "database_name": binding.database_name,
            "target_binding_sha256": target_binding_digest(binding),
            "observed_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
        },
    )
    return CleanupApprovalRequest(
        version="cleanup-approval-v1",
        id=uuid.uuid4(),
        scope="isolated-acceptance",
        expected_count=1,
        target=binding,
        runtime=runtime,
        recovery_receipt=receipt,
        approved_by="fixture-operator",
        reason="Synthetic exact-target approval",
        expires_at=now + timedelta(minutes=20),
    )


async def test_receipt_authorizer_dry_run_apply_idempotence_and_audit(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    approval = await private_approval_request(db, tmp_path, video, media)
    guard = type("Guard", (), {"check": AsyncMock(return_value=RUNTIME)})()
    service = CleanupAuthorizer(
        db, guard, staging_root=tmp_path, library_root=tmp_path / "library", allow_operator_exception=True
    )
    before = {p.name: p.stat() for p in tmp_path.iterdir()}
    assert (await service.authorize(approval))["result"] == "ELIGIBLE_FOR_AUTHORIZATION"
    for table in ("cleanup_authorizations", "cleanup_journal", "cleanup_audit"):
        assert await db.fetchval(f"SELECT count(*) FROM {table}") == 0  # noqa: S608 -- fixed names
    assert {p.name: p.stat() for p in tmp_path.iterdir()} == before
    assert (await service.authorize(approval, apply=True))["authorized_count"] == 1
    assert (await service.authorize(approval, apply=True))["result"] == "ALREADY_AUTHORIZED"
    with pytest.raises(CleanupApprovalError, match="ID_CONFLICT"):
        await service.authorize(approval.model_copy(update={"reason": "new consent"}), apply=True)
    assert await db.fetchval("SELECT count(*) FROM cleanup_authorizations") == 1
    assert media.exists()
    assert (await janitor(db, tmp_path).cleanup())["deleted"] == 1
    audit = json.loads(await reasons_for(db))["authorization"]
    intent = json.loads(await db.fetchval("SELECT gates FROM cleanup_journal"))["authorization"]
    assert audit == intent
    assert audit["authorization_id"] == str(approval.id)
    assert audit["recovery"]["receipt_sha256"] == approval.recovery_receipt.sha256
    assert not audit["recovery"]["off_host_attested"]


async def test_receipt_authorizer_requires_exception_opt_in_and_all_other_live_gates(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    approval = await private_approval_request(db, tmp_path, video, media)
    guard = type("Guard", (), {"check": AsyncMock(return_value=RUNTIME)})()
    service = CleanupAuthorizer(db, guard, staging_root=tmp_path, library_root=tmp_path / "library")
    with pytest.raises(CleanupApprovalError, match="EXCEPTION_NOT_ALLOWED"):
        await service.authorize(approval, apply=True)
    await db.execute("UPDATE videos SET local_cleanup_after=now()+interval '1 day' WHERE id=$1", video)
    service.allow_operator_exception = True
    with pytest.raises(CleanupApprovalError, match="LIVE_GATES_INCOMPLETE"):
        await service.authorize(approval, apply=True)
    assert await db.fetchval("SELECT count(*) FROM cleanup_authorizations") == 0
    assert media.exists()


class _OperatorEnvironment(dict):
    def __repr__(self):
        return "<private operator child environment>"


class _OperatorPrivateValues(tuple):
    def __repr__(self):
        return "<private operator output redaction values>"


async def _operator_child(module, arguments, environment, private_values=()):
    """Run the real entry point; never expose a child's unbounded output."""
    __tracebackhide__ = True
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        module,
        *arguments,
        cwd=Path.cwd(),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, errors = await asyncio.wait_for(process.communicate(), 90)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if errors or len(output) > 4096 or any(value.encode() in output for value in private_values):
        pytest.fail("operator child emitted unexpected or private output")
    try:
        value = json.loads(output)
    except (ValueError, TypeError):
        pytest.fail("operator child did not return bounded JSON")
    if not isinstance(value, dict):
        pytest.fail("operator child JSON must be an object")
    return process.returncode, value


async def _operator_domain_snapshot(db):
    rows = []
    for table in (
        "videos",
        "accounts",
        "remote_assets",
        "playable_assets",
        "library_publications",
        "cleanup_authorizations",
        "cleanup_journal",
        "cleanup_audit",
    ):
        rows.append(
            await db.fetchval(
                f"SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb) FROM {table} t"  # noqa: S608 -- fixed table names
            )
        )
    return rows


def _operator_file_metadata(root):
    result = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            info = path.stat()
            result[str(path.relative_to(root))] = (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_atime_ns,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
    return result


async def test_real_cleanup_cli_uses_live_instances_and_only_its_owned_pause(db, integration_redis, tmp_path):
    """A subprocess, actual Docker CLI/SDK, PG, Redis and private files.

    Domain media/restore attestations are synthetic; no provider or original
    deployment is connected. Authorization registers consent and never unlinks.
    """
    import docker
    from scripts.instance_guard import database_identity
    from scripts.integration_guard import require_test_redis

    assert await database_identity(db) == os.environ["PIXAV_TEST_DB_IDENTITY"]
    assert (await db.fetchval("SELECT current_database()")).startswith("pixav_test_")
    redis, pause_key = integration_redis
    await require_test_redis(redis)
    tmp_path.chmod(0o700)
    client = docker.from_env(timeout=15)
    try:
        daemon = client.info()["ID"]
    finally:
        client.close()
    context = (
        subprocess.run(  # noqa: S603 -- fixed read-only local Docker query
            [shutil.which("docker"), "context", "show"],
            capture_output=True,
            check=True,
            timeout=15,
        )
        .stdout.decode()
        .strip()
    )
    runtime = CleanupRuntimeBinding(
        docker_context=context,
        docker_daemon_id=daemon,
        redis_run_id=str((await redis.info("server"))["run_id"]),
    )
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    for path in tmp_path.rglob("*"):
        if path.is_file() and not path.is_symlink():
            os.utime(path, ns=(1_000_000_000, path.stat().st_mtime_ns))
    approval = await private_approval_request(db, tmp_path, video, media, runtime=runtime)

    def private_file(name, content):
        path = tmp_path / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(content)
        return str(path)

    request_path = private_file("request.json", approval.model_dump_json())
    wrong_daemon = approval.model_copy(
        update={"runtime": runtime.model_copy(update={"docker_daemon_id": "unrelated-synthetic-daemon"})}
    )
    wrong_daemon_path = private_file("wrong-daemon.json", wrong_daemon.model_dump_json())
    other_run_id = "0" * 40 if runtime.redis_run_id != "0" * 40 else "1" * 40
    wrong_redis = approval.model_copy(update={"runtime": runtime.model_copy(update={"redis_run_id": other_run_id})})
    wrong_redis_path = private_file("wrong-redis.json", wrong_redis.model_dump_json())
    wrong_database = approval.model_copy(
        update={
            "target": approval.target.model_copy(
                update={"database_identifier": str(int(approval.target.database_identifier) ^ 1)}
            )
        }
    )
    wrong_database_path = private_file("wrong-database.json", wrong_database.model_dump_json())
    token, wrong_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    token_path = private_file("owner-token", token)
    wrong_path = private_file("wrong-token", wrong_token)
    endpoint = urlsplit(os.environ["PIXAV_E2E_ADMIN_DSN"])
    environment = _OperatorEnvironment(
        {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(Path.cwd() / "src") + os.pathsep + str(Path.cwd()),
            "PIXAV_DB_HOST": endpoint.hostname,
            "PIXAV_DB_PORT": str(endpoint.port or 5432),
            "PIXAV_DB_USER": unquote(endpoint.username),
            "PIXAV_DB_PASSWORD": unquote(endpoint.password),
            "PIXAV_DB_NAME": approval.target.database_name,
            "PIXAV_EXPECTED_DB_IDENTITY": approval.target.database_identifier,
            "PIXAV_EXPECTED_DB_NAME": approval.target.database_name,
            "PIXAV_REDIS_URL": os.environ["PIXAV_E2E_REDIS_URL"],
            "PIXAV_EXPECTED_REDIS_RUN_ID": runtime.redis_run_id,
            "PIXAV_SYSTEM_PAUSE_KEY": pause_key,
            "PIXAV_DOWNLOAD_DIR": str(tmp_path),
            "PIXAV_LIBRARY_ROOT": str(tmp_path / "library"),
        }
    )
    private_values = _OperatorPrivateValues((token, wrong_token, unquote(endpoint.password)))

    async def cleanup(*arguments):
        return await _operator_child("scripts.manage_cleanup", list(arguments), environment, private_values)

    pause_arguments = ["--token-file", token_path]
    paused = False
    try:
        code, report = await _operator_child(
            "scripts.manage_cleanup",
            ["files", "--request", request_path, "--allow-operator-exception"],
            _OperatorEnvironment({**environment, "PIXAV_DB_PORT": "invalid-settings-must-not-be-loaded"}),
            private_values,
        )
        assert code == 0 and report == {
            "result": "FILES_VERIFIED_ONLY",
            "expected_count": 1,
            "authorized_count": 0,
            "recovery_mode": "operator-exception",
            "runtime_checked": False,
            "database_clock_checked": False,
        }
        code, report = await cleanup(
            "authorize",
            "--request",
            request_path,
            "--maintenance-token-file",
            token_path,
            "--allow-operator-exception",
            "--apply",
        )
        assert code == 2 and report == {"result": "REFUSED", "code": "CLEANUP_OWNED_SYSTEM_PAUSE_REQUIRED"}
        code, report = await _operator_child(
            "scripts.manage_system_pause", ["pause", *pause_arguments], environment, private_values
        )
        assert code == 0 and report["paused"] and report["owned"]
        paused = True
        pause_before = await redis.get(pause_key)
        database_before, files_before = await _operator_domain_snapshot(db), _operator_file_metadata(tmp_path)
        base = ["authorize", "--request", request_path, "--maintenance-token-file", token_path]
        code, report = await cleanup(*base)
        assert code == 2 and report == {"result": "REFUSED", "code": "CLEANUP_OPERATOR_EXCEPTION_NOT_ALLOWED"}
        code, report = await cleanup(
            "authorize",
            "--request",
            request_path,
            "--maintenance-token-file",
            wrong_path,
            "--allow-operator-exception",
            "--apply",
        )
        assert code == 2 and report == {"result": "REFUSED", "code": "CLEANUP_OWNED_SYSTEM_PAUSE_REQUIRED"}
        for transplanted, expected_code in (
            (wrong_daemon_path, "CLEANUP_RUNTIME_BINDING_MISMATCH"),
            (wrong_redis_path, "CLEANUP_AUTHORIZATION_UNCONFIRMED"),
            (wrong_database_path, "CLEANUP_AUTHORIZATION_UNCONFIRMED"),
        ):
            code, report = await cleanup(
                "authorize",
                "--request",
                transplanted,
                "--maintenance-token-file",
                token_path,
                "--allow-operator-exception",
                "--apply",
            )
            assert code == 2 and report == {"result": "REFUSED", "code": expected_code}
        code, report = await cleanup(*base, "--allow-operator-exception")
        assert code == 0 and report["result"] == "ELIGIBLE_FOR_AUTHORIZATION" and report["authorized_count"] == 0
        assert await _operator_domain_snapshot(db) == database_before
        assert _operator_file_metadata(tmp_path) == files_before
        assert await redis.get(pause_key) == pause_before
        code, report = await cleanup(*base, "--allow-operator-exception", "--apply")
        assert code == 0 and report["result"] == "AUTHORIZED" and report["authorized_count"] == 1
        code, report = await cleanup(*base, "--allow-operator-exception", "--apply")
        assert code == 0 and report["result"] == "ALREADY_AUTHORIZED" and report["authorized_count"] == 0
        assert await db.fetchval("SELECT count(*) FROM cleanup_authorizations") == 1
        assert await db.fetchval("SELECT count(*) FROM cleanup_journal") == 0
        assert await db.fetchval("SELECT count(*) FROM cleanup_audit") == 0
        assert await redis.get(pause_key) == pause_before and media.exists()
    finally:
        if paused:
            code, report = await _operator_child(
                "scripts.manage_system_pause", ["resume", *pause_arguments], environment, private_values
            )
            assert code == 0 and report["owned_pause_released"]


async def test_the_missing_producer_is_named_rather_than_assumed_bdd_005_131(db, tmp_path):
    """A refusal has to say which capability is absent, not just "not eligible"."""
    video, media = await artifact(db, tmp_path)
    await publish(db, video)

    await janitor(db, tmp_path).cleanup()

    reasons = await reasons_for(db)
    assert '"verified_playback_evidence": false' in reasons
    assert '"exact_target_backup_authorization": false' in reasons
    assert media.read_bytes() == b"the only local copy"


async def test_verified_playback_alone_does_not_authorize_deletion_bdd_058(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.exists()
    assert '"exact_target_backup_authorization": false' in await reasons_for(db)


async def test_ready_without_a_stream_check_is_not_playback_evidence_bdd_058(db, tmp_path):
    """Prepared is not played: only a recorded check through the stable URL counts."""
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_ready(db, video, verified=False)

    await janitor(db, tmp_path).cleanup()

    assert '"verified_playback_evidence": false' in await reasons_for(db)
    assert media.read_bytes() == b"the only local copy"


async def test_withdrawn_remote_copy_withdraws_playback_evidence_bdd_058_084(db, tmp_path):
    """Invalidating the remote asset takes every playable object built on it along."""
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    remote = await db.fetchval("SELECT id FROM remote_assets WHERE video_id=$1", video)

    await RemoteAssetRepository(db).invalidate(remote, "READBACK_FAILED")

    playable = await db.fetchrow("SELECT state, invalidated_reason FROM playable_assets WHERE video_id=$1", video)
    assert playable["state"] == "INVALID" and playable["invalidated_reason"] == "REMOTE_ASSET_INVALID"
    await janitor(db, tmp_path).cleanup()
    assert '"verified_playback_evidence": false' in await reasons_for(db)
    assert media.read_bytes() == b"the only local copy"


async def test_an_authorization_for_a_neighbouring_path_authorizes_nothing_bdd_058(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    neighbour = tmp_path / "unrelated.mp4"
    neighbour.write_bytes(b"not approved")
    await authorize(db, video, media, path=str(neighbour))

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.exists() and neighbour.read_bytes() == b"not approved"
    assert '"exact_target_backup_authorization": false' in await reasons_for(db)


async def test_an_authorization_for_bytes_that_changed_authorizes_nothing_bdd_058(db, tmp_path):
    """The approval is about content, and the janitor recomputes it from disk."""
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    media.write_bytes(b"a different local copy entirely")

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.exists()
    assert '"exact_target_backup_authorization": false' in await reasons_for(db)


async def test_an_expired_authorization_authorizes_nothing_bdd_058(db, tmp_path):
    """Approval is a window someone opened, not a standing permission."""
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    # The schema refuses a window that was already closed when it was written,
    # so the window is moved wholesale into the past instead.
    await db.execute("""UPDATE cleanup_authorizations
        SET created_at=now()-interval '2 hours', expires_at=now()-interval '1 hour'""")

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.exists()
    assert '"exact_target_backup_authorization": false' in await reasons_for(db)


async def test_an_authorization_loses_its_footing_when_the_asset_does_bdd_058(db, tmp_path):
    """Approval rests on a durable remote copy; withdraw that and it rests on nothing."""
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    await db.execute("UPDATE remote_assets SET state='INVALID',durable_at=NULL")

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.read_bytes() == b"the only local copy"
    reasons = await reasons_for(db)
    assert '"durable_remote_asset": false' in reasons
    assert '"exact_target_backup_authorization": false' in reasons


# ── Every guarantee at once ─────────────────────────────────────────────────


async def test_every_guarantee_together_removes_exactly_the_approved_target_bdd_058_063_134(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    neighbour = tmp_path / "unrelated.mp4"
    neighbour.write_bytes(b"not approved")

    preview = await janitor(db, tmp_path, apply=False).cleanup()
    assert preview["eligible"] == 1 and preview["deleted"] == 0 and media.exists()
    assert await db.fetchval("SELECT count(*) FROM cleanup_audit") == 0

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 1 and stats["rejected"] == 0
    assert not media.exists() and neighbour.read_bytes() == b"not approved"
    decision = await db.fetchrow("SELECT * FROM cleanup_audit")
    assert decision["decision"] == "deleted" and decision["applied"] is True and decision["path"] == str(media)
    assert all(json.loads(decision["reasons"]).values())
    # The domain no longer treats the local file as authoritative; the remote
    # copy and the playable object it backs are untouched.
    assert await db.fetchval("SELECT local_path FROM videos WHERE id=$1", video) is None
    assert await db.fetchval("SELECT state FROM remote_assets WHERE video_id=$1", video) == "DURABLE"
    assert await db.fetchval("SELECT state FROM playable_assets WHERE video_id=$1", video) == "READY"


@pytest.mark.parametrize("reference_kind", ["video", "alias", "workflow", "segment", "reader"])
async def test_other_video_referencing_the_file_blocks_cleanup_even_when_not_due(db, tmp_path, reference_kind):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other, other_media = await artifact(db, other_dir, due=False)
    if reference_kind in ("video", "alias"):
        path = str(media) if reference_kind == "video" else str(other_dir / ".." / media.name)
        await db.execute("UPDATE videos SET local_path=$1 WHERE id=$2", path, other)
    elif reference_kind == "workflow":
        await db.execute("UPDATE workflow_artifacts SET path=$1 WHERE path=$2", str(media), str(other_media))
    elif reference_kind == "segment":
        asset = await db.fetchval("SELECT id FROM remote_assets WHERE video_id=$1", other)
        await db.execute(
            """INSERT INTO remote_asset_segments(asset_id,segment_index,start_seconds,end_seconds,
            size_bytes,sha256,local_path,media_info) VALUES($1,0,0,1,19,$2,$3,'{}')""",
            asset,
            "d" * 64,
            str(media),
        )
    else:
        await db.execute(
            """INSERT INTO reader_leases(id,video_id,path,holder,lease_until)
            VALUES($1,$2,$3,'test-reader',now()+interval '1 hour')""",
            uuid.uuid4(),
            other,
            str(media),
        )
    before = await db.fetchval("SELECT count(*) FROM cleanup_audit")
    preview = await janitor(db, tmp_path, apply=False).cleanup()
    assert preview["rejected"] == 1 and preview["eligible"] == 0
    assert await db.fetchval("SELECT count(*) FROM cleanup_audit") == before
    stats = await janitor(db, tmp_path).cleanup()
    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.read_bytes() == b"the only local copy" and other_media.exists()
    assert '"single_video_path_reference": false' in await reasons_for(db)


async def test_path_changed_after_candidate_scan_preserves_both_files(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"new staging path")

    class ChangedAfterScan:
        async def fetch(self, query, *args):
            rows = await db.fetch(query, *args)
            await db.execute("UPDATE videos SET local_path=$1 WHERE id=$2", str(replacement), video)
            return rows

        def acquire(self):
            return db.acquire()

    stats = await janitor(ChangedAfterScan(), tmp_path).cleanup()
    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.exists() and replacement.read_bytes() == b"new staging path"
    assert await db.fetchval("SELECT local_path FROM videos WHERE id=$1", video) == str(replacement)
    assert '"local_path_unchanged": false' in await reasons_for(db)


@pytest.mark.parametrize("stage", ["before-intent", "after-intent", "after-unlink", "after-commit"])
async def test_cleanup_sigkill_recovery_uses_only_committed_intents(db, tmp_path, stage):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    database = await db.fetchval("SELECT current_database()")
    endpoint = urlsplit(os.environ["PIXAV_E2E_ADMIN_DSN"])
    dsn = urlunsplit(endpoint._replace(path=f"/{database}"))
    ready = tmp_path / "kill-ready"
    child = tmp_path / "cleanup-child.py"
    child.write_text(r"""
import asyncio, json, os, sys
from pathlib import Path
import asyncpg
from pixav.maxwell_core.gc import LocalFileJanitor
from pixav.shared.instance import database_identity
from pixav.shared.client_playback_evidence import ClientPlaybackEvidence
from pixav.shared.cleanup_recovery import CleanupRuntimeBinding

root, stage, ready = sys.argv[1:]
dsn = sys.stdin.readline().rstrip("\n")
async def checkpoint():
    Path(ready).write_text("ready")
    await asyncio.sleep(60)
class KillJanitor(LocalFileJanitor):
    async def _verified_playback(self, conn, video):
        row = await conn.fetchrow("SELECT *,now() AS checked_at FROM playable_assets WHERE video_id=$1 AND state='READY'", video)
        if row:
            client = ClientPlaybackEvidence.model_validate(json.loads(row["playback_evidence"])["client_playback"])
            Path(root, "playback-gate.json").write_text(json.dumps({
                "ready_at": row["ready_at"].isoformat(), "observed_at": client.observed_at.isoformat(),
                "checked_at": row["checked_at"].isoformat(), "binding_matches": client.matches(dict(row)),
            }))
        return await super()._verified_playback(conn, video)
    async def _persist_intent(self, *args):
        if stage == "before-intent": await checkpoint()
        result = await super()._persist_intent(*args)
        if stage == "after-intent": await checkpoint()
        return result
    async def _audit(self, conn, video, path, decision, applied, reasons):
        if stage == "after-unlink" and decision == "deleted": await checkpoint()
        await super()._audit(conn, video, path, decision, applied, reasons)
async def main():
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    try:
        assert await database_identity(pool) == os.environ["PIXAV_TEST_DB_IDENTITY"]
        assert (await pool.fetchval("SELECT current_database()")).startswith("pixav_test_")
        runtime = CleanupRuntimeBinding(docker_context="synthetic", docker_daemon_id="synthetic-daemon", redis_run_id="a"*40)
        await KillJanitor(pool, download_dir=root, library_root=root+"/library",
            recovery_runtime=runtime, allow_operator_exception=True).cleanup(apply=True)
        if stage == "after-commit": await checkpoint()
    finally: await pool.close()
asyncio.run(main())
""")
    process = subprocess.Popen(  # noqa: S603 -- guarded child uses only this test's database and files
        [sys.executable, str(child), str(tmp_path), stage, str(ready)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        process.stdin.write((dsn + "\n").encode())
        process.stdin.close()
        for _ in range(200):
            if ready.exists():
                break
            if process.poll() is not None:
                audit = await db.fetch("SELECT decision,reasons FROM cleanup_audit ORDER BY created_at")
                trace = tmp_path / "playback-gate.json"
                facts = trace.read_text() if trace.exists() else "missing playback trace"
                pytest.fail(
                    f"cleanup child exited before checkpoint: code={process.returncode}, audit={list(audit)}, facts={facts}"
                )
            await asyncio.sleep(0.05)
        assert ready.exists(), "cleanup did not reach crash checkpoint"
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGKILL)
        process.wait(timeout=10)
    assert media.exists() == (stage in ("before-intent", "after-intent"))
    assert await db.fetchval("SELECT count(*) FROM cleanup_journal") == (stage != "before-intent")
    operator = janitor(db, tmp_path)
    if stage == "after-unlink":
        assert await db.fetchval("SELECT local_path FROM videos WHERE id=$1", video) == str(media)
        assert await db.fetchval("SELECT count(*) FROM cleanup_audit") == 0
        assert (await operator.recover())["recovered"] == 1
        assert await db.fetchval("SELECT count(*) FROM cleanup_audit") == 0
        assert await db.fetchval("SELECT state FROM cleanup_journal") == "INTENT"
        assert (await operator.recover(apply=True))["recovered"] == 1
        assert await db.fetchval("SELECT local_path FROM videos WHERE id=$1", video) is None
        assert await db.fetchval("SELECT decision FROM cleanup_audit") == "missing"
        assert await db.fetchval("SELECT state FROM cleanup_journal") == "COMPLETED"
        assert (await operator.recover(apply=True))["recovered"] == 0
    elif stage == "after-intent":
        assert (await operator.recover(apply=True))["pending"] == 1
        assert media.exists()
        assert (await operator.cleanup())["deleted"] == 1
        assert await db.fetchval("SELECT count(*) FROM cleanup_journal") == 1
    elif stage == "before-intent":
        assert await operator.recover() == {"recovered": 0, "pending": 0, "rejected": 0}
    else:
        assert await db.fetchval("SELECT state FROM cleanup_journal") == "COMPLETED"
        assert await db.fetchval("SELECT count(*) FROM cleanup_audit") == 1
        assert (await operator.recover(apply=True))["recovered"] == 0


@pytest.mark.parametrize("invalid_time", ["future", "before-ready"])
async def test_client_evidence_outside_ready_and_db_clock_is_rejected(db, tmp_path, invalid_time):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    row = await db.fetchrow(
        "SELECT ready_at,playback_evidence,now() AS checked_at FROM playable_assets WHERE video_id=$1", video
    )
    evidence = json.loads(row["playback_evidence"])
    observed = (
        row["checked_at"] + timedelta(days=1) if invalid_time == "future" else row["ready_at"] - timedelta(days=1)
    )
    evidence["client_playback"]["observed_at"] = observed.isoformat()
    await db.execute(
        "UPDATE playable_assets SET playback_evidence=$2::jsonb WHERE video_id=$1", video, json.dumps(evidence)
    )
    assert (await janitor(db, tmp_path).cleanup())["rejected"] == 1
    assert media.exists()
    assert not json.loads(await reasons_for(db))["verified_playback_evidence"]
    assert await db.fetchval("SELECT count(*) FROM cleanup_journal") == 0


async def test_replacement_after_committed_intent_is_preserved(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)

    class ReplaceAfterIntent(LocalFileJanitor):
        async def _persist_intent(self, *args):
            result = await super()._persist_intent(*args)
            replacement = tmp_path / "replacement.mp4"
            replacement.write_bytes(media.read_bytes())
            replacement.replace(media)
            return result

    operator = ReplaceAfterIntent(
        db,
        download_dir=str(tmp_path),
        library_root=str(tmp_path / "library"),
        recovery_runtime=RUNTIME,
        allow_operator_exception=True,
    )
    assert (await operator.cleanup(apply=True))["failed"] == 1
    assert media.read_bytes() == b"the only local copy"
    assert await db.fetchval("SELECT state FROM cleanup_journal") == "INTENT"
    assert (await janitor(db, tmp_path).cleanup())["rejected"] == 1
    assert media.exists()
    assert (await operator.recover(apply=True))["pending"] == 1


async def test_publication_of_a_superseded_object_is_not_complete_bdd_005_131(db, tmp_path):
    """The library must stream the object that is READY now, not one it replaced."""
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    old = await db.fetchval("SELECT id FROM playable_assets WHERE video_id=$1", video)
    await db.execute("UPDATE playable_assets SET state='STALE' WHERE id=$1", old)
    await playback_verified(db, video)

    stats = await janitor(db, tmp_path).cleanup()

    assert stats["deleted"] == 0 and stats["rejected"] == 1
    assert media.read_bytes() == b"the only local copy"
    reasons = await reasons_for(db)
    assert '"complete_publication": false' in reasons and '"verified_playback_evidence": true' in reasons


async def test_deleted_poster_blocks_cleanup_even_with_published_row(db, tmp_path):
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    poster = tmp_path / "library" / "library" / f"pixav-{video}" / "poster.png"
    poster.unlink()
    async with db.acquire() as conn:
        gates = await janitor(db, tmp_path)._evaluate(conn, video, str(media))
    assert gates["complete_publication"] is False
    assert media.exists()


async def test_binding_expand_and_full_synthetic_restore_preserve_approvals(db, integration_redis, tmp_path):
    """A restored approval is preserved as history, not consent for another DB."""
    import asyncpg

    import docker
    from pixav.shared.instance import database_identity
    from scripts.integration_guard import require_test_database, require_test_redis

    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    await playback_verified(db, video)
    await authorize(db, video, media)
    legacy_id = await db.fetchval("SELECT id FROM cleanup_authorizations")
    await db.execute("ALTER TABLE cleanup_authorizations DROP COLUMN target_binding")
    await db.execute("ALTER TABLE cleanup_authorizations DROP COLUMN recovery_authorization")
    await db.execute("ALTER TABLE cleanup_authorizations DROP COLUMN approval_sha256")
    await db.execute(Path("migrations/025_cleanup_target_binding.sql").read_text())
    await db.execute(Path("migrations/026_cleanup_recovery_authorization.sql").read_text())
    assert await db.fetchval("SELECT target_binding FROM cleanup_authorizations WHERE id=$1", legacy_id) is None
    await authorize(db, video, media)
    approval_sql = (
        "SELECT id,target_binding,recovery_authorization,approval_sha256 FROM cleanup_authorizations ORDER BY id"
    )
    bindings = await db.fetch(approval_sql)
    assert len(bindings) == 2
    assert await db.fetchval(
        "SELECT has_column_privilege('pixav_execution_authority','cleanup_authorizations','target_binding','SELECT')"
    )
    assert not await db.fetchval(
        "SELECT has_table_privilege('pixav_execution_authority','cleanup_authorizations','INSERT')"
    )
    redis, _ = integration_redis
    await require_test_redis(redis)
    database = await db.fetchval("SELECT current_database()")
    endpoint = urlsplit(os.environ["PIXAV_E2E_ADMIN_DSN"])
    admin = await asyncpg.connect(os.environ["PIXAV_E2E_ADMIN_DSN"])
    target = "pixav_test_" + uuid.uuid4().hex
    client = docker.from_env(timeout=15)
    created = False
    try:
        await require_test_database(admin)
        containers = client.containers.list(
            all=True,
            filters={"label": ["com.docker.compose.project=pixav-integration", "com.docker.compose.service=postgres"]},
        )
        assert len(containers) == 1
        container = containers[0]
        assert not any(m["Type"] in ("bind", "volume") for m in container.attrs["Mounts"])
        identity = container.exec_run(
            [
                "psql",
                "-U",
                "pixav_test",
                "-d",
                "pixav_integration",
                "-Atc",
                "SELECT system_identifier FROM pg_control_system()",
            ]
        )
        assert identity.exit_code == 0 and identity.output.decode().strip() == await database_identity(db)
        docker_bin = shutil.which("docker")
        assert docker_bin is not None
        cli = subprocess.run(  # noqa: S603 -- fixed read-only Docker identity query
            [docker_bin, "info", "--format", "{{.ID}}"], capture_output=True, check=True, timeout=15
        )
        assert cli.stdout.decode().strip() == client.info()["ID"]
        dump = subprocess.run(  # noqa: S603 -- exact guarded CI container and synthetic database
            [docker_bin, "exec", container.id, "pg_dump", "-U", "pixav_test", "-Fc", database],
            capture_output=True,
            check=True,
            timeout=30,
        )
        await admin.execute(f'CREATE DATABASE "{target}"')
        created = True
        subprocess.run(  # noqa: S603 -- restore only this test's freshly created database
            [docker_bin, "exec", "-i", container.id, "pg_restore", "-U", "pixav_test", "-d", target, "--exit-on-error"],
            input=dump.stdout,
            capture_output=True,
            check=True,
            timeout=30,
        )
        restored = await asyncpg.connect(urlunsplit(endpoint._replace(path=f"/{target}")))
        try:
            assert await database_identity(restored) == await database_identity(admin)
            assert await restored.fetch(approval_sql) == bindings
            assert not await janitor(restored, tmp_path)._authorized_target(restored, video, str(media))
            assert media.exists()
        finally:
            await restored.close()
    finally:
        try:
            if created:
                await require_test_database(admin)
                await require_test_redis(redis)
                assert cli.stdout.decode().strip() == client.info()["ID"]
                await admin.execute(f'DROP DATABASE "{target}"')
        finally:
            await admin.close()
            client.close()
