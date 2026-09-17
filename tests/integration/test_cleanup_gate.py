"""Real PostgreSQL cleanup authorization.

Staging deletion is the one irreversible step in the pipeline, so these tests
are written from the refusals inward: every guarantee is removed in turn and the
artifact must survive. A stand-in publication table cannot authorize deletion:
verified playback and exact-target backup authorization are not integrated yet.
"""

import hashlib
import uuid
from pathlib import Path

import pytest

from pixav.maxwell_core.gc import LocalFileJanitor

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
    """Stand in for LibraryProjection so the approval path can be exercised."""
    await db.execute("""CREATE TABLE IF NOT EXISTS library_publications (
            video_id uuid PRIMARY KEY REFERENCES videos(id), state text NOT NULL)""")
    await db.execute(
        """INSERT INTO library_publications(video_id,state) VALUES($1,'PUBLISHED')
        ON CONFLICT (video_id) DO UPDATE SET state='PUBLISHED'""",
        video,
    )


def janitor(db, tmp_path, *, apply=True):
    return LocalFileJanitor(db, download_dir=str(tmp_path), apply_deletions=apply)


async def test_projection_absent_always_rejects_bdd_005_131(db, tmp_path):
    """Component D/E have not landed, so nothing is cleanup-eligible yet."""
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


async def playback_verified(db, video):
    """Stand in for PlaybackResolver so the other half can be exercised alone."""
    await db.execute("""CREATE TABLE IF NOT EXISTS playable_assets (
            video_id uuid PRIMARY KEY REFERENCES videos(id), state text NOT NULL)""")
    await db.execute(
        """INSERT INTO playable_assets(video_id,state) VALUES($1,'READY')
        ON CONFLICT (video_id) DO UPDATE SET state='READY'""",
        video,
    )


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
