"""The migration runner has to find ``migrations/`` from inside the package.

``migrations/`` is repository data, not wheel data: ``pyproject.toml`` packages
only ``src/pixav``. Moving the runner out of ``scripts/`` therefore changed the
expression that locates it, and a wrong expression fails silently — globbing a
missing directory yields nothing and the runner reports success having applied
nothing. These tests pin both the location and the refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pixav.shared.migrations import MIGRATIONS_DIR, run_migrations


class TestMigrationsDirectory:
    def test_resolves_to_the_repository_migrations_directory(self) -> None:
        assert MIGRATIONS_DIR.is_dir(), f"{MIGRATIONS_DIR} does not exist"

    def test_contains_the_migration_the_single_film_run_stops_at(self) -> None:
        """`first_4k_movie.py` passes `until="011_first_4k_heartbeat.sql"`."""
        assert (MIGRATIONS_DIR / "011_first_4k_heartbeat.sql").is_file()

    def test_sits_beside_src_rather_than_inside_it(self) -> None:
        """A path one level short would land on `src/migrations`, which no one creates."""
        assert (MIGRATIONS_DIR.parent / "src" / "pixav").is_dir()


class TestMissingDirectory:
    @pytest.mark.asyncio
    async def test_refuses_to_report_success_when_the_directory_is_gone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Without this the runner connects, finds no files and logs "0 applied"."""
        monkeypatch.setattr("pixav.shared.migrations.MIGRATIONS_DIR", tmp_path / "absent")

        with pytest.raises(FileNotFoundError, match="migrations directory not found"):
            await run_migrations("postgresql://unused/unused")

    @pytest.mark.asyncio
    async def test_checks_the_directory_before_opening_a_connection(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A bad deployment should fail on its own evidence, not on a connect timeout."""
        monkeypatch.setattr("pixav.shared.migrations.MIGRATIONS_DIR", tmp_path / "absent")

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("connected before checking the migrations directory")

        monkeypatch.setattr("pixav.shared.migrations.asyncpg.connect", refuse)

        with pytest.raises(FileNotFoundError):
            await run_migrations("postgresql://unused/unused")
