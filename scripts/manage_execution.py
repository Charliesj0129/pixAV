#!/usr/bin/env python3
"""Inspect and manually recover managed executions, and withdraw lost assets.

Reading is always safe. Replay, cancel, resume, retry-now and reopen are
explicit operator acts that require a name and a reason, and they create
auditable new history rather than rewriting what happened: the original
execution and its attempts are preserved. Resume additionally refuses to clear
any recovery fact that records a remote effect, so releasing a held execution
can never redo one blindly. Reopen gives a diagnosed infrastructure failure its
attempt budget back on the same execution, asset and recovery journal, which is
what keeps a fixed fault from being answered with a replay -- a second upload of
media that is already uploaded.

``invalidate-asset`` is the one act here that is not about an execution: it
withdraws a durability claim for a remote copy the operator has confirmed is
gone. A share location that merely stopped resolving is not that, and the
pipeline recovers from it on its own.

Nothing here prints a credential. The output is built from the frozen Execution
and ExecutionAttempt models, which carry classifications and identifiers only;
the raw checkpoint is never printed because it holds local filesystem paths.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from typing import Any

from pixav.config import get_settings
from pixav.maxwell_core.media_workflow import MediaWorkflow
from pixav.maxwell_core.storage_workflow import StorageWorkflow
from pixav.shared.db import create_pool
from pixav.shared.instance import database_identity

OPEN_STATES = ("READY", "RUNNING", "WAITING_RETRY", "WAITING_QUOTA", "USER_ACTION_REQUIRED")


def _render(execution: Any) -> dict:
    """Operator-visible facts only: state, classification and identity."""
    payload = execution.model_dump(mode="json")
    payload["attempts"] = [
        {
            "generation": attempt["generation"],
            "stage": attempt["stage"],
            "operation_id": attempt["operation_id"],
            "started_at": attempt["started_at"],
            "completed_at": attempt["completed_at"],
            "outcome": attempt["outcome"],
            "error_code": attempt["error_code"],
        }
        for attempt in payload.get("attempts", [])
    ]
    return payload


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    pool = await create_pool(settings)
    try:
        identity = await database_identity(pool)
        if args.db_identity and args.db_identity != identity:
            print(f"refusing: database identity is {identity}, expected {args.db_identity}")
            return 2
        workflow = MediaWorkflow(pool)

        if args.command == "list":
            states = tuple(args.state) if args.state else OPEN_STATES
            rows = await pool.fetch(
                """SELECT e.id, e.state, e.stage, e.failure_class, e.error_code, e.blocked_reason,
                e.due_at, t.video_id FROM executions e JOIN workflow_tasks t ON t.task_id=e.task_id
                WHERE e.state = ANY($1::text[]) ORDER BY e.due_at, e.created_at LIMIT $2""",
                list(states),
                args.limit,
            )
            print(json.dumps([{k: str(v) for k, v in dict(row).items()} for row in rows], indent=2))
            return 0

        if args.command == "inspect":
            execution = await workflow.inspect(args.execution_id)
            if execution is None:
                print(f"execution not found: {args.execution_id}")
                return 2
            print(json.dumps({"database_identity": identity, **_render(execution)}, indent=2))
            return 0

        # The remaining commands write. Echo the identity so the operator can
        # check which cluster is about to change before reading the result.
        print(f"database identity: {identity}")
        try:
            return await _write(args, workflow, pool)
        except ValueError as exc:
            print(f"refused: {exc}")
            return 2
    finally:
        await pool.close()


async def _write(args: argparse.Namespace, workflow: MediaWorkflow, pool: Any) -> int:
    """Operator acts. Each one refuses a state it has no business changing."""
    if args.command == "resume":
        await StorageWorkflow(pool).resume_after_user_action(
            args.execution_id,
            operator=args.operator,
            reason=args.reason,
            cleared=tuple(args.clear or ()),
        )
        print(f"resumed execution {args.execution_id}")
        return 0
    if args.command == "retry-now":
        await workflow.retry_now(args.execution_id, operator=args.operator, reason=args.reason)
        print(f"next attempt of {args.execution_id} is now due")
        return 0
    if args.command == "reopen":
        await workflow.reopen(args.execution_id, operator=args.operator, reason=args.reason)
        print(f"reopened execution {args.execution_id}")
        return 0
    if args.command == "invalidate-asset":
        previous = await StorageWorkflow(pool).invalidate_asset(
            args.asset_id, operator=args.operator, reason=args.reason
        )
        print(f"remote asset {args.asset_id} was {previous}; it is now INVALID")
        return 0
    if args.command == "replay":
        new_id = await workflow.replay(args.execution_id, operator=args.operator, reason=args.reason)
        print(f"replay of {args.execution_id} created execution {new_id}")
        return 0
    if not await workflow.cancel(args.execution_id, operator=args.operator, reason=args.reason):
        print(f"execution {args.execution_id} is already terminal; nothing was cancelled")
        return 2
    print(f"cancelled execution {args.execution_id}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-identity", default=None, help="refuse to act on a different PostgreSQL cluster")
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="list executions by state")
    listing.add_argument("--state", action="append", help="repeatable; defaults to every open state")
    listing.add_argument("--limit", type=int, default=50)

    inspection = commands.add_parser("inspect", help="show one execution with its attempt history")
    inspection.add_argument("execution_id", type=uuid.UUID)

    # Addresses a remote asset, not an execution, so it stands apart from the
    # execution acts below rather than sharing their argument shape.
    invalidation = commands.add_parser(
        "invalidate-asset",
        help="withdraw a durability claim after confirming the remote copy is gone",
    )
    invalidation.add_argument("asset_id", type=uuid.UUID)
    invalidation.add_argument("--operator", required=True, help="who established that the copy is gone")
    invalidation.add_argument("--reason", required=True, help="what they observed, recorded with the asset")

    for name, help_text in (
        ("replay", "start a new audited execution"),
        ("cancel", "stop an open execution"),
        ("resume", "release an execution held for user action, after inspecting the device"),
        ("retry-now", "bring a waiting execution's next attempt forward, after fixing the cause"),
        ("reopen", "return a retry-exhausted infrastructure failure to READY, after fixing the cause"),
    ):
        action = commands.add_parser(name, help=help_text)
        action.add_argument("execution_id", type=uuid.UUID)
        action.add_argument("--operator", required=True, help="who is taking responsibility for this")
        action.add_argument("--reason", required=True, help="why, recorded with the new history")
        if name == "resume":
            action.add_argument(
                "--clear",
                action="append",
                help="repeatable; a credential guard your inspection found unsent. Only "
                "credential guards may be named -- recovery facts about remote effects are refused.",
            )

    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
