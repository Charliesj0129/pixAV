"""Operator surface for the library projection (BDD-095, 096, 098, 103)."""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

import pytest

import scripts.manage_library as cli
from pixav.library_projection.reconciler import Decision

IDENTITY = "7682619096198328359"
VIDEO = uuid.UUID("12121212-1212-4121-8121-121212121212")
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic poster"


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
    def __init__(self) -> None:
        self.calls: list = []

    async def set_override(self, video_id, field, value, *, operator, reason):
        self.calls.append(("set", video_id, field, value, operator, reason))
        return value

    async def clear_override(self, video_id, field, *, operator, reason):
        self.calls.append(("clear", video_id, field, operator, reason))
        return True

    async def record_artwork(self, video_id, kind, **facts):
        self.calls.append(("artwork", video_id, kind, facts))
        return uuid.uuid4()

    async def overrides(self, video_id):
        return {"title": "Manual"}

    async def retained_artwork(self, video_id):
        return []


class Reconciler:
    def __init__(self) -> None:
        self.applied = False

    async def plan(self):
        return [Decision(VIDEO, "publish"), Decision(uuid.uuid4(), "unchanged")]

    async def reconcile(self):
        self.applied = True
        return {"published": 1}


class Settings:
    def __init__(self, tmp_path: Path) -> None:
        self.artwork_root = str(tmp_path / "artwork")


@pytest.fixture()
def wire(monkeypatch, tmp_path):
    def install(pool: Pool):
        repository, reconciler = Repository(), Reconciler()

        async def factory(*_args, **_kwargs):
            return pool

        monkeypatch.setattr(cli, "get_settings", lambda: Settings(tmp_path))
        monkeypatch.setattr(cli, "create_pool", factory)
        monkeypatch.setattr(cli, "LibraryRepository", lambda _pool: repository)
        monkeypatch.setattr(cli, "build_reconciler", lambda _pool, _settings: reconciler)
        return repository, reconciler

    return install


def args(command: str, **changes) -> argparse.Namespace:
    fields = {
        "command": command,
        "db_identity": None,
        "video_id": VIDEO,
        "limit": 50,
        "operator": "operator",
        "reason": "catalogue fix",
        "apply": False,
    }
    return argparse.Namespace(**{**fields, **changes})


async def test_override_set_records_operator_and_reason_bdd_095(wire, capsys):
    repository, _ = wire(Pool())

    assert await cli._run(args("override", action="set", field="title", value="Manual Title")) == 0

    assert repository.calls == [("set", VIDEO, "title", "Manual Title", "operator", "catalogue fix")]
    assert IDENTITY in capsys.readouterr().out


async def test_list_overrides_take_a_json_array(wire, capsys):
    repository, _ = wire(Pool())

    assert await cli._run(args("override", action="set", field="tags", value='["a", "b"]')) == 0
    assert repository.calls[0][3] == ["a", "b"]

    assert await cli._run(args("override", action="set", field="tags", value="a,b")) == 2
    assert "JSON array" in capsys.readouterr().out


async def test_override_clear_returns_the_field_to_the_providers_bdd_096(wire, capsys):
    repository, _ = wire(Pool())

    assert await cli._run(args("override", action="clear", field="studio")) == 0
    assert repository.calls == [("clear", VIDEO, "studio", "operator", "catalogue fix")]


async def test_artwork_import_retains_the_original_with_provenance_bdd_098(wire, tmp_path, capsys):
    repository, _ = wire(Pool())
    source = tmp_path / "poster.png"
    source.write_bytes(PNG)

    assert await cli._run(args("artwork", action="import", kind="poster", path=source)) == 0

    [(_, video_id, kind, facts)] = repository.calls
    assert (video_id, kind, facts["media_type"]) == (VIDEO, "poster", "image/png")
    assert facts["provenance"] == {"source": "operator-import", "operator": "operator", "reason": "catalogue fix"}
    assert str(tmp_path) not in capsys.readouterr().out.split("\n", 1)[1], "no local path is echoed"


async def test_a_file_that_is_not_an_image_is_refused(wire, tmp_path, capsys):
    repository, _ = wire(Pool())
    source = tmp_path / "poster.txt"
    source.write_text("not an image")

    assert await cli._run(args("artwork", action="import", kind="poster", path=source)) == 2
    assert repository.calls == [] and "refused" in capsys.readouterr().out


async def test_reconcile_is_a_dry_run_unless_applied_bdd_103(wire, capsys):
    _, reconciler = wire(Pool())

    assert await cli._run(args("reconcile")) == 0
    printed = capsys.readouterr().out
    assert f"{VIDEO}: publish" in printed and "unchanged" not in printed and not reconciler.applied

    assert await cli._run(args("reconcile", apply=True)) == 0
    assert reconciler.applied and '"published": 1' in capsys.readouterr().out


async def test_writes_refuse_a_different_cluster(wire, capsys):
    repository, _ = wire(Pool(identity="1111"))

    assert await cli._run(args("override", action="set", field="title", value="x", db_identity="2222")) == 2
    assert repository.calls == [] and "refusing" in capsys.readouterr().out


async def test_status_for_one_video_shows_its_decisions(wire, capsys):
    pool = Pool(rows=[{"video_id": VIDEO, "state": "PUBLISHED", "active_version": "1-aaaaaaaaaaaa"}])
    wire(pool)

    assert await cli._run(args("status")) == 0

    printed = capsys.readouterr().out
    assert "PUBLISHED" in printed and '"title": "Manual"' in printed and pool.closed
