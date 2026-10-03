"""Real PostgreSQL cleanup authorization.

Staging deletion is the one irreversible step in the pipeline, so these tests
are written from the refusals inward: every guarantee is removed in turn and the
artifact must survive. Publication, playable objects and stream checks are real
rows, written the way LibraryProjection and PlaybackResolver write them; only
the last tests hold every guarantee at once, and only then is a file removed.
"""

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from pixav.library_projection.artwork import ArtworkStore
from pixav.library_projection.metadata import EffectiveMetadata
from pixav.library_projection.render import render_projection
from pixav.library_projection.repository import ArtworkRecord
from pixav.library_projection.writer import ProjectionWriter
from pixav.maxwell_core.gc import LocalFileJanitor
from pixav.shared.remote_assets import RemoteAssetRepository

pytestmark = pytest.mark.integration


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
        db, download_dir=str(tmp_path), library_root=str(tmp_path / "library"), apply_deletions=apply
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
            ready_at,materialized_at) VALUES($1,$2,$3,'photos-playback-v1','READY',19,$4,now(),now())""",
            playable,
            video,
            remote,
            "d" * 64,
        )
    if verified:
        observed = await db.fetchval("SELECT now()")
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
            "UPDATE playable_assets SET playback_verified_at=now(),playback_evidence=$2::jsonb WHERE id=$1",
            playable,
            json.dumps({"client_playback": client}),
        )
    return playable


async def playback_verified(db, video):
    """PlaybackResolver's end-to-end stream check, so the other half can be exercised alone."""
    await playback_ready(db, video, verified=True)


async def authorize(db, video, media, **overrides):
    """Write one exact-target cleanup authorization for this artifact."""
    fields = {
        "path": str(media),
        "artifact_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
        "size_bytes": media.stat().st_size,
        "expires": "now()+interval '1 hour'",
    }
    fields.update(overrides)
    asset = await db.fetchval("SELECT id FROM remote_assets WHERE video_id=$1", video)
    await db.execute(
        f"""INSERT INTO cleanup_authorizations(id,video_id,path,artifact_sha256,size_bytes,remote_asset_id,
        playback_verified_at,backup_reference,approved_by,reason,expires_at)
        VALUES($1,$2,$3,$4,$5,$6,now(),'offsite-vault','fixture-operator','integration fixture',
        {fields["expires"]})""",  # noqa: S608 - interval literal comes from this module only
        uuid.uuid4(),
        video,
        fields["path"],
        fields["artifact_sha256"],
        fields["size_bytes"],
        asset,
    )


async def reasons_for(db):
    return await db.fetchval("SELECT reasons FROM cleanup_audit ORDER BY created_at DESC LIMIT 1")


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


async def test_publication_of_a_superseded_object_is_not_complete_bdd_005_131(db, tmp_path):
    """The library must stream the object that is READY now, not one it replaced."""
    video, media = await artifact(db, tmp_path)
    await publish(db, video)
    old = await db.fetchval("SELECT id FROM playable_assets WHERE video_id=$1", video)
    await db.execute("UPDATE playable_assets SET state='STALE' WHERE id=$1", old)
    await playback_verified(db, video)
    await authorize(db, video, media)

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
