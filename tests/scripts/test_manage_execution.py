"""Operator recovery surface for managed executions (BDD-127, 128, 129)."""

from __future__ import annotations

import argparse
import uuid
from datetime import datetime, timezone

import pytest

import scripts.manage_execution as cli
from pixav.shared.workflow import Execution, ExecutionAttempt

EXECUTION = uuid.UUID("55555555-5555-4555-8555-555555555555")
IDENTITY = "7682619096198328359"


def execution(**changes) -> Execution:
    fields = {
        "id": EXECUTION,
        "task_id": uuid.UUID("66666666-6666-4666-8666-666666666666"),
        "state": "FAILED",
        "stage": "upload",
        "generation": 3,
        "due_at": datetime(2026, 9, 15, tzinfo=timezone.utc),
        "infrastructure_retries": 6,
        "max_retries": 6,
        "recovery_count": 1,
        "failure_class": "infrastructure",
        "error_code": "DEPENDENCY_FAILURE",
        "attempts": (
            ExecutionAttempt(
                id=uuid.uuid4(),
                operation_id=uuid.uuid4(),
                generation=3,
                stage="upload",
                started_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
                outcome="infrastructure",
                error_code="DEPENDENCY_FAILURE",
            ),
        ),
    }
    return Execution(**{**fields, **changes})


class Pool:
    def __init__(self, identity: str = IDENTITY, rows=()):
        self.identity = identity
        self.rows = rows
        self.closed = False

    async def fetchval(self, query, *args):
        assert "pg_control_system" in query
        return self.identity

    async def fetch(self, query, *args):
        return list(self.rows)

    async def close(self):
        self.closed = True


class Workflow:
    def __init__(self, found=None, replay_error=None):
        self.found = found
        self.replay_error = replay_error
        self.calls: list = []

    async def inspect(self, execution_id):
        self.calls.append(("inspect", execution_id))
        return self.found

    async def replay(self, execution_id, *, operator, reason):
        self.calls.append(("replay", execution_id, operator, reason))
        if self.replay_error:
            raise self.replay_error
        return EXECUTION

    async def cancel(self, execution_id, *, operator, reason):
        self.calls.append(("cancel", execution_id, operator, reason))
        return self.found is not None


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


def args(command: str, **changes) -> argparse.Namespace:
    fields = {
        "command": command,
        "db_identity": None,
        "execution_id": EXECUTION,
        "operator": "operator",
        "reason": "manual recovery",
        "state": None,
        "limit": 50,
    }
    return argparse.Namespace(**{**fields, **changes})


async def test_inspect_reports_classification_and_identifiers_bdd_127(wire, capsys):
    pool, _ = wire(Pool(), Workflow(found=execution()))

    assert await cli._run(args("inspect")) == 0

    printed = capsys.readouterr().out
    assert "DEPENDENCY_FAILURE" in printed and "infrastructure" in printed
    assert str(EXECUTION) in printed
    assert pool.closed


async def test_inspect_exposes_no_secret_or_local_path_bdd_127(wire, capsys):
    """Execution carries classifications, never a checkpoint or a credential."""
    wire(Pool(), Workflow(found=execution()))
    await cli._run(args("inspect"))

    printed = capsys.readouterr().out
    for forbidden in ("checkpoint", "password", "cookie", "/data/", "owner"):
        assert forbidden not in printed


async def test_unknown_execution_is_reported_not_invented(wire, capsys):
    wire(Pool(), Workflow(found=None))

    assert await cli._run(args("inspect")) == 2
    assert "not found" in capsys.readouterr().out


async def test_replay_records_operator_and_reason_bdd_128(wire, capsys):
    _, workflow = wire(Pool(), Workflow(found=execution()))

    assert await cli._run(args("replay")) == 0

    assert workflow.calls == [("replay", EXECUTION, "operator", "manual recovery")]
    assert str(EXECUTION) in capsys.readouterr().out


async def test_replay_of_a_live_execution_is_refused_bdd_129(wire, capsys):
    """Only the authority decides what may be replayed; the CLI reports its answer."""
    wire(Pool(), Workflow(replay_error=ValueError("only failed or cancelled execution may be manually replayed")))

    assert await cli._run(args("replay")) == 2
    assert "refused" in capsys.readouterr().out


async def test_writes_refuse_a_different_cluster(wire, capsys):
    _, workflow = wire(Pool(identity="1111"), Workflow(found=execution()))

    assert await cli._run(args("replay", db_identity="2222")) == 2

    assert workflow.calls == [], "no write was attempted against the wrong cluster"
    assert "refusing" in capsys.readouterr().out


async def test_writes_echo_the_cluster_they_are_about_to_change(wire, capsys):
    wire(Pool(), Workflow(found=execution()))

    await cli._run(args("cancel"))

    assert IDENTITY in capsys.readouterr().out


async def test_cancelling_a_terminal_execution_changes_nothing(wire, capsys):
    wire(Pool(), Workflow(found=None))

    assert await cli._run(args("cancel")) == 2
    assert "already terminal" in capsys.readouterr().out


async def test_list_defaults_to_the_open_states(wire, capsys):
    row = {"id": EXECUTION, "state": "WAITING_QUOTA", "stage": "upload", "video_id": uuid.uuid4()}
    wire(Pool(rows=[row]), Workflow())

    assert await cli._run(args("list")) == 0
    assert "WAITING_QUOTA" in capsys.readouterr().out
