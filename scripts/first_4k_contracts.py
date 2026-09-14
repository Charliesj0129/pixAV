"""Recovery contracts for the isolated single-film CLI; no production dispatch."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from pixav.pixel_injector.canary import CanaryBlockedError

FIXED_DEFAULTS = {
    "max_threads": 30,
    "max_movie_gib": 80,
    "min_movie_seconds": 3600,
    "min_movie_gib": 8,
    "fetch_attachments": False,
}


def resolve_configuration(args: Any, state: dict, runtime: dict, default_board: str) -> dict:
    """Distinguish omission from drift; never invent missing legacy evidence."""
    defaults = {"board": default_board, **FIXED_DEFAULTS}
    saved = state.get("configuration")
    legacy = bool(state.get("candidates") or state.get("discovery_intent") or state.get("video_id"))
    if saved is None and legacy:
        saved = state.get("configuration_evidence")
        if not isinstance(saved, dict) or set(saved) != set(defaults) | {"runtime"}:
            raise CanaryBlockedError("legacy configuration evidence incomplete; retained run requires reconciliation")
    if saved is not None:
        if set(saved) != set(defaults) | {"runtime"} or saved["runtime"] != runtime:
            raise CanaryBlockedError("saved profile, segmentation policy or image identity changed")
        for key in defaults:
            explicit = getattr(args, key, None)
            if explicit is not None and explicit != saved[key]:
                raise CanaryBlockedError(f"resume configuration differs: {key}")
        resolved = dict(saved)
    else:
        resolved = {key: getattr(args, key, None) for key in defaults}
        resolved = {key: defaults[key] if value is None else value for key, value in resolved.items()}
        resolved["runtime"] = runtime
    for key in defaults:
        setattr(args, key, resolved[key])
    return resolved


class RunHeartbeat:
    """One run's liveness, with a sticky failure checked before further effects."""

    def __init__(
        self,
        pool: Any,
        run_id: Any,
        state: dict,
        *,
        interval: float = 30,
        tolerance: float = 120,
        lock_connection: Any = None,
    ) -> None:
        self.pool, self.id, self.state, self.interval = pool, run_id, state, interval
        self.lock_connection = lock_connection
        self.tolerance = tolerance
        self.failed = threading.Event()
        self.last_ok = time.monotonic()
        self.progress = False

    def check(self) -> None:
        if self.failed.is_set() or time.monotonic() - self.last_ok > self.tolerance:
            self.failed.set()
            raise CanaryBlockedError("run heartbeat unavailable or stale; no further external effects")

    async def pulse(self, *, transient_ok: bool = False) -> None:
        """Record liveness; a transient database fault leaves ``last_ok`` unmoved.

        ``check()`` already tolerates ``tolerance`` seconds since the last
        successful pulse, but latching failure on the first error made that
        window unreachable: one five second Postgres crash recovery ended an
        eight hour segmentation run that was five parts from finishing. Only a
        fault that cannot heal on its own stays sticky, and the window itself
        decides when an outage has lasted too long.
        """
        if self.lock_connection is not None:
            try:
                held = await asyncio.wait_for(
                    self.lock_connection.fetchval("""SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid()
                           AND locktype='advisory' AND classid=0 AND objid=410041004
                           AND objsubid=1 AND granted)"""),
                    timeout=10,
                )
                if not held:
                    raise CanaryBlockedError("dedicated run advisory lock lost")
            except (Exception, asyncio.CancelledError):
                # A reconnected heartbeat pool cannot restore session ownership.
                self.failed.set()
                raise
        try:
            tag = await asyncio.wait_for(
                self.pool.execute(
                    """UPDATE first_4k_runs SET heartbeat_at=clock_timestamp(),
                       stage_started_at=CASE WHEN heartbeat_stage IS DISTINCT FROM $2
                         THEN clock_timestamp() ELSE stage_started_at END,
                       progress_at=CASE WHEN $4 THEN clock_timestamp() ELSE progress_at END,
                       heartbeat_stage=$2,current_part_index=$3 WHERE id=$1""",
                    self.id,
                    self.state["stage"],
                    self.state.get("current_part_index"),
                    self.progress,
                ),
                timeout=10,
            )
        except asyncio.CancelledError:
            self.failed.set()
            raise
        except Exception:
            if not transient_ok:
                self.failed.set()
                raise
            return
        if tag != "UPDATE 1":
            # A run row that is gone will not come back by waiting.
            self.failed.set()
            raise CanaryBlockedError("heartbeat run row missing")
        self.last_ok = time.monotonic()
        self.progress = False

    @asynccontextmanager
    async def watch(self):
        await self.pulse()
        parent = asyncio.current_task()

        async def loop() -> None:
            try:
                while True:
                    await asyncio.sleep(self.interval)
                    await self.pulse(transient_ok=True)
                    if time.monotonic() - self.last_ok > self.tolerance:
                        raise CanaryBlockedError("run heartbeat stale beyond tolerance")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failed.set()
                if parent is not None:
                    parent.cancel()

        task = asyncio.create_task(loop())
        try:
            yield
        except asyncio.CancelledError:
            if self.failed.is_set():
                raise CanaryBlockedError("heartbeat failed; run retained for reconciliation") from None
            raise
        finally:
            # A cancelled to_thread await does not stop its worker. The sticky
            # guard also stops any remaining media process when the scope exits.
            self.failed.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def read_status(conn: Any, run_id: Any | None = None) -> dict:
    """Safe on migration 010 too; never emit the private run document."""
    async with conn.transaction(readonly=True):
        if not await conn.fetchval("SELECT to_regclass('public.first_4k_runs')"):
            return {"status": "NO_RUN"}
        rows = await conn.fetch(
            """SELECT to_jsonb(r) AS row,
               extract(epoch FROM clock_timestamp() - (to_jsonb(r)->>'heartbeat_at')::timestamptz) AS heartbeat_age
               FROM first_4k_runs r WHERE ($1::uuid IS NULL OR id=$1)""",
            run_id,
        )
        if not rows:
            return {"status": "NO_RUN"}
        if len(rows) != 1:
            raise CanaryBlockedError("multiple film runs require explicit run ID")
        row = json.loads(rows[0]["row"])
        state = row["document"]
        heartbeat = row.get("heartbeat_at")
        # JSON timestamp fractions vary in width; Python 3.10 fromisoformat
        # rejects some valid PostgreSQL values. Keep age on the DB clock.
        age = rows[0]["heartbeat_age"]
        video = state.get("video_id")
        if video is None:
            selected = [c["video_id"] for c in state.get("candidates", []) if c.get("video_id")]
            video = selected[0] if len(selected) == 1 else None
        counts = await conn.fetchrow(
            """SELECT count(*) AS parts, count(usage_counted_at) AS confirmed,
               max(uploaded_at) + interval '24 hours' AS quota_due
               FROM video_parts WHERE video_id=$1::text::uuid""",
            video,
        )
        return {
            "status": "STATUS",
            "run_id": row["id"],
            "stage": state.get("stage"),
            "liveness": "UNKNOWN" if age is None else ("STALE" if age > 120 else "RECENT"),
            "heartbeat_at": heartbeat,
            "stage_started_at": row.get("stage_started_at"),
            "progress_at": row.get("progress_at"),
            "current_part_index": row.get("current_part_index"),
            "parts": counts["parts"],
            "confirmed": counts["confirmed"],
            "quota_due": counts["quota_due"].isoformat() if counts["quota_due"] else None,
            "gates": state.get("gates", {}),
        }
