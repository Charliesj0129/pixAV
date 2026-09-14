"""Tests for the destructive-script instance guard.

The scenario these encode is the one CLAUDE.md warns about: on this host,
`localhost:5432` and the compose network's `postgres:5432` are different
PostgreSQL instances. A cleanup that plans against one and applies against the
other prints a completely plausible report while deleting rows nobody looked at.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.instance_guard import (
    METADATA_SUFFIX,
    database_identity,
    metadata_path,
    read_backup_identity,
    redis_identity,
    require_same_instance,
    write_backup_metadata,
)

LIVE = "7606585154138693670"
OTHER = "1234567890123456789"


@pytest.fixture
def backup(tmp_path: Path) -> Path:
    path = tmp_path / "pixav-20260831T000000Z.dump"
    path.write_bytes(b"PGDMP")
    return path


class TestIdentityReads:
    async def test_database_identity_reads_pg_control_system(self) -> None:
        conn = AsyncMock()
        conn.fetchval.return_value = 7606585154138693670

        assert await database_identity(conn) == LIVE
        assert "pg_control_system" in conn.fetchval.await_args.args[0]

    async def test_redis_identity_reads_the_run_id(self) -> None:
        redis = AsyncMock()
        redis.info.return_value = {"run_id": "abc123"}

        assert await redis_identity(redis) == "abc123"

    async def test_missing_run_id_is_empty_not_an_error(self) -> None:
        redis = AsyncMock()
        redis.info.return_value = {}

        assert await redis_identity(redis) == ""


class TestBackupMetadata:
    def test_sidecar_sits_beside_the_backup(self, backup: Path) -> None:
        assert metadata_path(backup).name == backup.name + METADATA_SUFFIX

    def test_written_sidecar_round_trips(self, backup: Path) -> None:
        write_backup_metadata(backup, system_identifier=LIVE, database="pixav")

        assert read_backup_identity(backup) == LIVE

    def test_sidecar_is_owner_only(self, backup: Path) -> None:
        """It names the production cluster; same treatment as the dump itself."""
        target = write_backup_metadata(backup, system_identifier=LIVE, database="pixav")

        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_absent_sidecar_reads_as_unknown(self, backup: Path) -> None:
        assert read_backup_identity(backup) is None

    def test_corrupt_sidecar_reads_as_unknown_rather_than_matching(self, backup: Path) -> None:
        metadata_path(backup).write_text("{not json", encoding="utf-8")

        assert read_backup_identity(backup) is None


class TestRequireSameInstance:
    def test_matching_identifier_is_accepted(self, backup: Path) -> None:
        write_backup_metadata(backup, system_identifier=LIVE, database="pixav")

        require_same_instance(backup=backup, live_identifier=LIVE, allow_unverified=False)

    def test_mismatch_is_refused(self, backup: Path) -> None:
        write_backup_metadata(backup, system_identifier=OTHER, database="pixav")

        with pytest.raises(RuntimeError, match="came from PostgreSQL system identifier"):
            require_same_instance(backup=backup, live_identifier=LIVE, allow_unverified=False)

    def test_mismatch_is_refused_even_with_the_override(self, backup: Path) -> None:
        """The override covers "unknown", never "known to be a different cluster"."""
        write_backup_metadata(backup, system_identifier=OTHER, database="pixav")

        with pytest.raises(RuntimeError, match="came from PostgreSQL system identifier"):
            require_same_instance(backup=backup, live_identifier=LIVE, allow_unverified=True)

    def test_missing_sidecar_is_refused_by_default(self, backup: Path) -> None:
        with pytest.raises(RuntimeError, match="no .meta.json sidecar"):
            require_same_instance(backup=backup, live_identifier=LIVE, allow_unverified=False)

    def test_missing_sidecar_is_allowed_with_the_explicit_override(self, backup: Path) -> None:
        require_same_instance(backup=backup, live_identifier=LIVE, allow_unverified=True)

    def test_sidecar_records_the_database_name_too(self, backup: Path) -> None:
        target = write_backup_metadata(backup, system_identifier=LIVE, database="pixav")

        assert json.loads(target.read_text(encoding="utf-8"))["database"] == "pixav"
