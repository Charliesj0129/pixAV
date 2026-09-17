"""Operator entry point for an operator-supplied canary upload."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from contextlib import asynccontextmanager

import pytest

import scripts.canary_upload as cli

IDENTITY = "7617854039601979430"
EXECUTION = uuid.UUID("55555555-5555-4555-8555-555555555555")
ASSET = uuid.UUID("66666666-6666-4666-8666-666666666666")
SHARE_URL = "https://photos.app.goo.gl/managedcanary"
STAGED = "/app/data/storage-staging/asset/part.mp4"


class Connection:
    def __init__(self, rows: dict) -> None:
        self.rows = rows
        self.executed: list = []

    async def fetchval(self, query, *args):
        self.executed.append((query, args))
        if "INSERT INTO videos" in query:
            return self.rows["video_id"]
        if "SELECT id FROM executions" in query:
            return EXECUTION
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        if "FROM executions" in query:
            return self.rows.get("execution")
        if "FROM remote_assets " in query:
            return self.rows.get("asset")
        raise AssertionError(query)

    async def fetch(self, query, *args):
        return list(self.rows.get("segments", ()))


class Pool:
    def __init__(self, rows: dict, identity: str = IDENTITY) -> None:
        self.connection = Connection(rows)
        self.identity = identity
        self.closed = False

    async def fetchval(self, query, *args):
        assert "pg_control_system" in query
        return self.identity

    @asynccontextmanager
    async def acquire(self):
        yield self.connection

    async def close(self):
        self.closed = True


class Workflow:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list = []

    async def admit(self, video_id, **_kwargs):
        self.calls.append(("admit", video_id))
        return uuid.UUID("77777777-7777-4777-8777-777777777777")

    async def adopt_local_source(self, execution_id, **kwargs):
        self.calls.append(("adopt", execution_id, kwargs))
        if self.error:
            raise self.error
        return uuid.UUID("88888888-8888-4888-8888-888888888888")


@pytest.fixture()
def wire(monkeypatch):
    def install(pool: Pool, workflow: Workflow):
        monkeypatch.setattr(cli, "get_settings", lambda: object())
        monkeypatch.setattr(cli, "create_pool", _async(pool))
        monkeypatch.setattr(cli, "MediaWorkflow", lambda _pool: workflow)
        return pool, workflow

    return install


def _async(value):
    async def factory(*_args, **_kwargs):
        return value

    return factory


def source(tmp_path):
    path = tmp_path / "canary.mp4"
    path.write_bytes(b"canary bytes")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def args(command: str, **changes) -> argparse.Namespace:
    fields = {
        "command": command,
        "db_identity": None,
        "path": None,
        "sha256": None,
        "title": "managed canary",
        "source_url": "https://example.invalid/canary.mp4",
        "operator": "tester",
        "reason": "managed upload canary",
        "execution_id": EXECUTION,
    }
    return argparse.Namespace(**{**fields, **changes})


async def test_admit_records_the_operator_supplied_provenance(wire, tmp_path, capsys):
    path, digest = source(tmp_path)
    pool, workflow = wire(Pool({"video_id": uuid.uuid4()}), Workflow())

    assert await cli._run(args("admit", path=str(path), sha256=digest)) == 0

    printed = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert printed["provenance"] == "operator-supplied"
    assert printed["execution_id"] == str(EXECUTION)
    adopt = next(call for call in workflow.calls if call[0] == "adopt")
    assert adopt[2]["declared_sha256"] == digest
    assert adopt[2]["operator"] == "tester"
    assert pool.closed


async def test_a_different_cluster_is_refused_before_anything_is_written(wire, tmp_path, capsys):
    path, digest = source(tmp_path)
    _, workflow = wire(Pool({"video_id": uuid.uuid4()}), Workflow())

    code = await cli._run(args("admit", path=str(path), sha256=digest, db_identity="1234567890"))

    assert code == 2
    assert "refusing" in capsys.readouterr().out
    assert workflow.calls == []


async def test_a_rejected_adoption_is_reported_not_retried(wire, tmp_path, capsys):
    path, digest = source(tmp_path)
    wire(Pool({"video_id": uuid.uuid4()}), Workflow(error=ValueError("source file does not match")))

    assert await cli._run(args("admit", path=str(path), sha256=digest)) == 2
    assert "refused" in capsys.readouterr().out


def test_a_missing_or_symlinked_source_is_refused(tmp_path):
    target, _ = source(tmp_path)
    link = tmp_path / "link.mp4"
    link.symlink_to(target)

    with pytest.raises(SystemExit):
        cli._source(str(link))
    with pytest.raises(SystemExit):
        cli._source(str(tmp_path / "absent.mp4"))


async def test_status_reports_durability_facts_without_the_share_location(wire, capsys):
    rows = {
        "execution": {
            "id": EXECUTION,
            "state": "SUCCEEDED",
            "stage": "verify",
            "generation": 4,
            "blocked_reason": None,
            "failure_class": None,
            "error_code": None,
            "checkpoint": json.dumps({"asset_id": str(ASSET), "prepared_path": STAGED}),
        },
        "asset": {"state": "DURABLE", "durable_at": "2026-09-15T00:00:00+00:00"},
        "segments": [
            {
                "segment_index": 0,
                "state": "verified",
                "size_bytes": 128_743_122,
                "sha256": "a" * 64,
                "usage_counted_at": "2026-09-15T00:00:00+00:00",
                "uploaded_at": "2026-09-15T00:00:00+00:00",
                "share_url": SHARE_URL,
                "verification": json.dumps(
                    {"readback": {"method": "photos-original-browser", "cold_inputs": "provider-only"}}
                ),
            }
        ],
    }
    wire(Pool(rows), Workflow())

    assert await cli._run(args("status")) == 0

    printed = capsys.readouterr().out
    report = json.loads(printed.split("\n", 1)[1])
    assert report["asset_state"] == "DURABLE"
    assert report["segments"][0]["has_share_url"] is True
    assert report["segments"][0]["readback_cold_inputs"] == "provider-only"
    assert SHARE_URL not in printed
    assert STAGED not in printed
