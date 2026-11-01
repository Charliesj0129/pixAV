"""Operator hold on one upload that has not been dispatched yet.

The system pause stops every stage, and an account cooldown is cleared again
by the next usage record, so neither can keep one upload parked while another
execution finishes its verification. A hold moves the upload into the existing
``USER_ACTION_REQUIRED`` state, which nothing dispatches, and the existing
``resume`` act releases it. No segment, recovery journal or remote effect is
touched, so a held upload resumes exactly where it was.
"""

from __future__ import annotations

import json
import uuid

import asyncpg

OPERATOR_UPLOAD_HOLD = "OPERATOR_UPLOAD_HOLD"
HOLDABLE_STATES = ("READY", "WAITING_QUOTA", "WAITING_RETRY")


async def hold_upload(pool: asyncpg.Pool, execution_id: uuid.UUID, *, operator: str, reason: str) -> None:
    """Park an upload that no worker holds. A running upload is refused, never fenced mid-push."""
    if not operator.strip() or not reason.strip():
        raise ValueError("hold requires operator and reason")
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT * FROM executions WHERE id=$1 FOR UPDATE", execution_id)
        if row is None or row["stage"] != "upload":
            raise ValueError("only an upload execution may be held")
        if row["state"] not in HOLDABLE_STATES:
            raise ValueError("only an upload that is not running or held may be held")
        checkpoint = json.loads(row["checkpoint"])
        checkpoint["operator_upload_hold"] = {"operator": operator, "reason": reason, "state_before": row["state"]}
        # The generation bump fences any request built before this hold.
        await conn.execute(
            """UPDATE executions SET state='USER_ACTION_REQUIRED',blocked_reason=$2,owner=NULL,lease_until=NULL,
            generation=generation+1,checkpoint=$3::jsonb,updated_at=now() WHERE id=$1""",
            execution_id,
            OPERATOR_UPLOAD_HOLD,
            json.dumps(checkpoint),
        )
        await conn.execute("DELETE FROM account_leases WHERE execution_id=$1", execution_id)
