"""Real private files and explicit cleanup receipt/exception trust boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pixav.maxwell_core.cleanup_authorization import CleanupAuthorizer
from pixav.maxwell_core.gc import _GATE_CONDITIONS, LocalFileJanitor
from pixav.shared.cleanup_binding import CleanupTargetBinding
from pixav.shared.cleanup_recovery import (
    CleanupApprovalError,
    CleanupApprovalRequest,
    CleanupAuthorizationEvidence,
    CleanupRuntimeBinding,
    EvidenceFile,
    cleanup_approval_digest,
    read_cleanup_request,
    recovery_authorizes,
    target_binding_digest,
    validate_cleanup_recovery,
)
from pixav.shared.media_file import MediaFile, open_media_file

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
RUNTIME = CleanupRuntimeBinding(docker_context="synthetic", docker_daemon_id="synthetic-daemon", redis_run_id="a" * 40)


def artifact(root, name, value):
    raw = json.dumps(value).encode() if isinstance(value, dict) else value
    path = root / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
    return EvidenceFile(path=str(path), size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def target(root):
    path = root / "synthetic.mp4"
    if not path.exists():
        artifact(root, path.name, b"original")
    file, parent = path.stat(), root.stat()
    return CleanupTargetBinding(
        version="cleanup-target-v1",
        database_identifier="123",
        database_name="synthetic_db",
        video_id=uuid.uuid4(),
        path=str(path),
        file={
            "device": file.st_dev,
            "inode": file.st_ino,
            "size": file.st_size,
            "mtime_ns": file.st_mtime_ns,
            "ctime_ns": file.st_ctime_ns,
            "parent_device": parent.st_dev,
            "parent_inode": parent.st_ino,
        },
        artifact_sha256=hashlib.sha256(b"original").hexdigest(),
        remote_asset_id=uuid.uuid4(),
        playable_asset_id=uuid.uuid4(),
        preparation_version="synthetic-v1",
        playable_sha256="c" * 64,
        playable_size=8,
        playback_verified_at=NOW - timedelta(hours=1),
        publication_version="1-aaaaaaaaaaaa",
        publication_digest="d" * 64,
    )


def request(root, *, mode="operator-exception", scope="isolated-acceptance", mutate=None):
    root.chmod(0o700)
    binding = target(root)
    body = {
        "kind": mode,
        "database_identifier": "123",
        "database_name": "synthetic_db",
        "target_binding_sha256": target_binding_digest(binding),
        "runtime": RUNTIME.model_dump(),
        "observed_at": (NOW - timedelta(minutes=10)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
    }
    if mode == "operator-exception":
        body.update(
            scope="isolated-acceptance",
            authorized_by="fixture-operator",
            reason="explicit synthetic exception",
            instruction=artifact(
                root, "instruction.txt", b"Synthetic operator explicitly waives a backup."
            ).model_dump(),
        )
    else:
        snapshot = str(uuid.uuid4())
        inventory = {
            "version": "cleanup-inventory-v1",
            "snapshot_id": snapshot,
            "database_identifier": "123",
            "database_name": "synthetic_db",
            "target_binding_sha256": target_binding_digest(binding),
            "runtime": RUNTIME.model_dump(),
            "contents": {
                key: {"sha256": "e" * 64, "count": 1}
                for key in ("database", "roles", "ledger", "queue", "guest", "watch_history", "selected_rows")
            },
        }
        body.update(
            method="independent-full-restore-v1",
            snapshot_id=snapshot,
            off_host_attested=scope == "production",
            encrypted_backup=artifact(
                root, "synthetic.age", b"synthetic archive fixture; no actual backup is made"
            ).model_dump(),
            source_sidecar=artifact(
                root,
                "source.meta.json",
                {
                    "schema_version": 1,
                    "backup": "synthetic.age",
                    "system_identifier": "123",
                    "database": "synthetic_db",
                },
            ).model_dump(),
            source_inventory=artifact(root, "source.json", inventory).model_dump(),
            restored_inventory=artifact(root, "restored.json", inventory).model_dump(),
        )
    if mutate:
        mutate(body)
    receipt = artifact(root, "receipt.json", body)
    return CleanupApprovalRequest(
        version="cleanup-approval-v1",
        id=uuid.uuid4(),
        scope=scope,
        expected_count=1,
        target=binding,
        runtime=RUNTIME,
        recovery_receipt=receipt,
        approved_by="fixture-operator",
        reason="synthetic cleanup approval",
        expires_at=NOW + timedelta(minutes=20),
    )


def test_exception_requires_separate_opt_in_and_never_counts_as_restore(tmp_path):
    approval = request(tmp_path)
    with pytest.raises(CleanupApprovalError, match="EXCEPTION_NOT_ALLOWED"):
        validate_cleanup_recovery(approval, checked_at=NOW)
    stamp = validate_cleanup_recovery(approval, checked_at=NOW, allow_operator_exception=True)
    assert stamp.mode == "operator-exception" and not stamp.off_host_attested
    assert not recovery_authorizes(stamp, approval.target, RUNTIME, checked_at=NOW)
    assert recovery_authorizes(stamp, approval.target, RUNTIME, checked_at=NOW, allow_operator_exception=True)


def test_exception_cannot_promote_to_production(tmp_path):
    approval = request(tmp_path, scope="production")
    with pytest.raises(CleanupApprovalError, match="EXCEPTION_NOT_ALLOWED"):
        validate_cleanup_recovery(approval, checked_at=NOW, allow_operator_exception=True)


def test_restore_checks_archive_sidecar_and_all_preservation_areas(tmp_path):
    approval = request(tmp_path, mode="verified-restore", scope="production")
    stamp = validate_cleanup_recovery(approval, checked_at=NOW)
    assert stamp.mode == "verified-restore" and stamp.off_host_attested
    assert recovery_authorizes(stamp, approval.target, RUNTIME, checked_at=NOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("database_identifier", "999"),
        ("database_name", "another_db"),
        ("target_binding_sha256", "f" * 64),
        ("runtime", {**RUNTIME.model_dump(), "redis_run_id": "f" * 40}),
        ("observed_at", (NOW + timedelta(seconds=1)).isoformat()),
        ("expires_at", (NOW - timedelta(seconds=1)).isoformat()),
        ("authorized_by", "another-operator"),
    ],
)
def test_receipt_cannot_be_transplanted_or_used_outside_its_window(tmp_path, field, value):
    approval = request(tmp_path, mutate=lambda body: body.update({field: value}))
    with pytest.raises(CleanupApprovalError):
        validate_cleanup_recovery(approval, checked_at=NOW, allow_operator_exception=True)


@pytest.mark.parametrize("file", ["receipt.json", "instruction.txt"])
def test_changed_private_evidence_is_rejected(tmp_path, file):
    approval = request(tmp_path)
    (tmp_path / file).write_bytes(b"changed after approval")
    with pytest.raises(CleanupApprovalError, match="EVIDENCE_CHANGED"):
        validate_cleanup_recovery(approval, checked_at=NOW, allow_operator_exception=True)


@pytest.mark.parametrize("fault", ["wrong-instance", "inventory-change", "missing-area", "same-inode", "no-off-host"])
def test_restore_refuses_incomplete_or_nonindependent_evidence(tmp_path, fault):
    def mutate(body):
        if fault == "no-off-host":
            body["off_host_attested"] = False
        elif fault == "same-inode":
            body["restored_inventory"] = body["source_inventory"]
        else:
            key = "source_sidecar" if fault == "wrong-instance" else "restored_inventory"
            payload = json.loads(
                (tmp_path / ("source.meta.json" if key == "source_sidecar" else "restored.json")).read_text()
            )
            if fault == "wrong-instance":
                payload["system_identifier"] = "999"
            elif fault == "inventory-change":
                payload["contents"]["queue"]["sha256"] = "f" * 64
            else:
                del payload["contents"]["guest"]
            body[key] = artifact(tmp_path, "replacement.json", payload).model_dump()

    approval = request(tmp_path, mode="verified-restore", scope="production", mutate=mutate)
    with pytest.raises(CleanupApprovalError):
        validate_cleanup_recovery(approval, checked_at=NOW)


def test_private_validation_is_read_only_including_atime(tmp_path):
    approval = request(tmp_path)
    ref = artifact(tmp_path, "request.json", approval.model_dump(mode="json"))
    for path in tmp_path.iterdir():
        os.utime(path, ns=(1_000_000_000, path.stat().st_mtime_ns))
    before = {
        p.name: (p.stat().st_size, p.stat().st_atime_ns, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
        for p in tmp_path.iterdir()
    }
    parsed = read_cleanup_request(tmp_path / "request.json")
    validate_cleanup_recovery(parsed, checked_at=NOW, allow_operator_exception=True)
    after = {
        p.name: (p.stat().st_size, p.stat().st_atime_ns, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
        for p in tmp_path.iterdir()
    }
    assert before == after and parsed == approval and ref.size_bytes > 0


def test_request_rejects_duplicate_json_fields_and_public_permissions(tmp_path):
    raw = tmp_path / "request.json"
    raw.write_text('{"version":"first","version":"second"}')
    raw.chmod(0o600)
    tmp_path.chmod(0o700)
    with pytest.raises(CleanupApprovalError, match="DUPLICATE"):
        read_cleanup_request(raw)
    raw.chmod(0o644)
    with pytest.raises(CleanupApprovalError, match="PRIVATE_FILE"):
        read_cleanup_request(raw)


def test_request_refuses_an_uncooperative_change_during_read(tmp_path, monkeypatch):
    approval = request(tmp_path)
    artifact(tmp_path, "request.json", approval.model_dump(mode="json"))
    original = MediaFile.reader

    @contextmanager
    def changed_after_read(source):
        with original(source) as reader:
            yield reader
        info = source.path.stat()
        os.utime(source.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))

    monkeypatch.setattr(MediaFile, "reader", changed_after_read)
    with pytest.raises(CleanupApprovalError, match="EVIDENCE_CHANGED"):
        read_cleanup_request(tmp_path / "request.json")


def test_target_digest_is_stable_across_equivalent_timezones(tmp_path):
    binding = target(tmp_path)
    later = binding.model_copy(
        update={"playback_verified_at": binding.playback_verified_at.astimezone(timezone(timedelta(hours=8)))}
    )
    assert target_binding_digest(binding) == target_binding_digest(later)


class ApprovalDB:
    """Transaction boundary double; inode locking and file verification are real.

    PostgreSQL gate queries and migration checks have separate opt-in tests.
    This double checks rollback, idempotence and the commit/lock ordering.
    """

    def __init__(self, approval):
        self.target = approval.target
        self.now = NOW
        self.schema = True
        self.rows = {}
        self.pending = []
        self.commits = 0

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        try:
            yield
            with pytest.raises(BlockingIOError), open_media_file(self.target.path, exclusive=True):
                pytest.fail("inode lock released before approval commit")
            self.rows.update(self.pending)
            self.commits += 1
        finally:
            self.pending.clear()

    async def fetchval(self, sql, *args):
        if "clock_timestamp" in sql:
            return self.now
        if "information_schema" in sql:
            return self.schema
        assert "local_path FROM videos" in sql
        return self.target.path

    async def fetchrow(self, sql, *args):
        if "current_database()" in sql:
            return {
                key: value
                for key, value in dict(self.target).items()
                if key not in ("version", "path", "file", "artifact_sha256")
            }
        assert "approval_sha256 FROM cleanup_authorizations" in sql
        return self.rows.get(args[0])

    async def execute(self, sql, *args):
        if "pg_advisory" in sql:
            return "SELECT 1"
        assert "INSERT INTO cleanup_authorizations" in sql
        self.pending.append((args[0], {"id": args[0], "approval_sha256": args[-1], "values": args}))
        return "INSERT 0 1"


def authorization_service(tmp_path, monkeypatch):
    approval = request(tmp_path)
    db = ApprovalDB(approval)
    guard = type("Guard", (), {"check": AsyncMock(return_value=RUNTIME)})()
    gates = {key: key != "exact_target_backup_authorization" for key in _GATE_CONDITIONS}
    evaluate = AsyncMock(side_effect=lambda *args: dict(gates))
    monkeypatch.setattr(LocalFileJanitor, "_evaluate", evaluate)
    service = CleanupAuthorizer(
        db, guard, staging_root=tmp_path, library_root=tmp_path / "library", allow_operator_exception=True
    )
    return service, approval, db, guard, gates


async def test_authorizer_dry_run_keeps_files_and_all_persistent_state(tmp_path, monkeypatch):
    service, approval, db, guard, _ = authorization_service(tmp_path, monkeypatch)
    before = {p.name: p.stat() for p in tmp_path.iterdir()}
    outcome = await service.authorize(approval)
    assert outcome["result"] == "ELIGIBLE_FOR_AUTHORIZATION"
    assert outcome["authorized_count"] == 0 and not db.rows and not db.pending
    assert {p.name: p.stat() for p in tmp_path.iterdir()} == before
    assert guard.check.await_count == 2 and db.commits == 1


async def test_authorizer_inserts_one_bound_receipt_and_repeating_is_idempotent(tmp_path, monkeypatch):
    service, approval, db, _, _ = authorization_service(tmp_path, monkeypatch)
    assert (await service.authorize(approval, apply=True))["authorized_count"] == 1
    row = db.rows[approval.id]
    stamp = json.loads(row["values"][-2])
    assert json.loads(row["values"][-3]) == approval.target.model_dump(mode="json")
    assert stamp["mode"] == "operator-exception" and not stamp["off_host_attested"]
    assert stamp["receipt_sha256"] == approval.recovery_receipt.sha256
    assert (await service.authorize(approval, apply=True))["result"] == "ALREADY_AUTHORIZED"
    assert len(db.rows) == 1 and Path(approval.target.path).read_bytes() == b"original"


async def test_authorizer_reused_request_id_cannot_change_consent(tmp_path, monkeypatch):
    service, approval, db, _, _ = authorization_service(tmp_path, monkeypatch)
    await service.authorize(approval, apply=True)
    changed = approval.model_copy(update={"reason": "different consent"})
    with pytest.raises(CleanupApprovalError, match="ID_CONFLICT"):
        await service.authorize(changed, apply=True)
    assert len(db.rows) == 1


@pytest.mark.parametrize("fault", ["replacement", "schema", "gates", "runtime-before", "runtime-after", "expiry"])
async def test_authorizer_refuses_live_changes_without_inserting(tmp_path, monkeypatch, fault):
    service, approval, db, guard, gates = authorization_service(tmp_path, monkeypatch)
    if fault == "replacement":
        replacement = tmp_path / "replacement.mp4"
        replacement.write_bytes(b"original")
        replacement.replace(approval.target.path)
    elif fault == "schema":
        db.schema = False
    elif fault == "gates":
        gates["no_active_reader"] = False
    elif fault.startswith("runtime"):
        changed = RUNTIME.model_copy(update={"redis_run_id": "f" * 40})
        guard.check.side_effect = [changed] if fault.endswith("before") else [RUNTIME, changed]
    else:

        async def after_guard():
            if guard.check.await_count == 2:
                db.now = approval.expires_at
            return RUNTIME

        guard.check.side_effect = after_guard
    with pytest.raises(CleanupApprovalError):
        await service.authorize(approval, apply=True)
    assert not db.rows and not db.pending and Path(approval.target.path).exists()


def committed_intent(tmp_path):
    approval = request(tmp_path)
    stamp = validate_cleanup_recovery(approval, checked_at=NOW, allow_operator_exception=True)
    authorization = CleanupAuthorizationEvidence(
        version="cleanup-authorization-evidence-v1",
        authorization_id=approval.id,
        approval_sha256=cleanup_approval_digest(approval),
        target=approval.target,
        recovery=stamp,
    )
    gates = {key: True for key in _GATE_CONDITIONS}
    gates.update(domain_path=approval.target.path, authorization=authorization.model_dump(mode="json"))
    row = {"id": uuid.uuid4(), "video_id": approval.target.video_id, "path": approval.target.path, "created_at": NOW}
    fingerprint = {**approval.target.file.model_dump(), "sha256": approval.target.artifact_sha256}
    return authorization, row, gates, fingerprint


async def test_intent_recovery_uses_committed_consent_clock_and_current_database_identity(tmp_path):
    authorization, row, gates, fingerprint = committed_intent(tmp_path)
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value={"identity": "123", "database": "synthetic_db"}))
    # A confirmed new daemon may reconcile an old committed unlink. Expiry
    # today does not invalidate the authorization valid at the intent's commit.
    runtime = RUNTIME.model_copy(update={"redis_run_id": "f" * 40})
    janitor = LocalFileJanitor(
        None, download_dir=str(tmp_path), recovery_runtime=runtime, allow_operator_exception=True
    )
    assert await janitor._intent_authorization(conn, row, gates, fingerprint) == authorization


@pytest.mark.parametrize("fault", ["legacy", "database", "video", "path", "fingerprint", "gate", "clock", "unbound"])
async def test_intent_recovery_never_backfills_missing_or_changed_consent(tmp_path, fault):
    _, row, gates, fingerprint = committed_intent(tmp_path)
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value={"identity": "123", "database": "synthetic_db"}))
    if fault == "legacy":
        del gates["authorization"]
    elif fault == "database":
        conn.fetchrow.return_value["database"] = "another_database"
    elif fault == "video":
        row["video_id"] = uuid.uuid4()
    elif fault == "path":
        row["path"] = str(tmp_path / "another.mp4")
    elif fault == "fingerprint":
        fingerprint["inode"] += 1
    elif fault == "gate":
        gates["durable_remote_asset"] = False
    elif fault == "clock":
        row["created_at"] = NOW + timedelta(hours=1)
    janitor = LocalFileJanitor(
        None,
        download_dir=str(tmp_path),
        recovery_runtime=None if fault == "unbound" else RUNTIME,
        allow_operator_exception=True,
    )
    assert await janitor._intent_authorization(conn, row, gates, fingerprint) is None


async def test_pending_intent_cannot_be_reused_with_a_new_authorization_id(tmp_path, monkeypatch):
    authorization, row, gates, fingerprint = committed_intent(tmp_path)
    pool = ApprovalDB(SimpleNamespace(target=authorization.target))
    pool.fetchrow = AsyncMock(return_value={**row, "fingerprint": json.dumps(fingerprint), "gates": json.dumps(gates)})
    janitor = LocalFileJanitor(
        pool, download_dir=str(tmp_path), recovery_runtime=RUNTIME, allow_operator_exception=True
    )
    changed = authorization.model_copy(update={"authorization_id": uuid.uuid4()})
    monkeypatch.setattr(janitor, "_authorization_evidence", AsyncMock(return_value=changed))
    with open_media_file(authorization.target.path, exclusive=True) as source:
        with pytest.raises(ValueError, match="pending cleanup intent"):
            await janitor._persist_intent(
                None, row["video_id"], row["path"], Path(row["path"]), gates, source.descriptor
            )
    assert not pool.rows and not pool.pending
