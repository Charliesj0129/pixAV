"""Helper to run a FastAPI health app alongside a worker coroutine.

Usage::

    from pixav.shared.health_server import run_with_health
    from pixav.shared.health import create_health_app

    health_app = create_health_app("my_module")
    await run_with_health(
        worker_coro=run_loop(settings),
        health_app=health_app,
        host=settings.health_host,
        port=settings.my_module_health_port,
    )
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Coroutine
from typing import Any

import uvicorn
from fastapi import FastAPI

from pixav.shared.health import HealthState

logger = logging.getLogger(__name__)


async def run_with_health(  # noqa: C901
    *,
    worker_coro: Coroutine[Any, Any, Any],
    health_app: FastAPI,
    host: str = "0.0.0.0",  # noqa: S104
    port: int = 8001,
    health_state: HealthState | None = None,
    heartbeat_interval_seconds: float = 5.0,
    watchdog_exit: Any = os._exit,
) -> None:
    """Run *worker_coro* and a uvicorn health server concurrently.

    If either coroutine exits (e.g. the worker returns after a signal), the
    other is cancelled so the process terminates cleanly.
    """
    config = uvicorn.Config(
        health_app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    logger.info("health server starting on %s:%d", host, port)

    state = health_state or getattr(health_app.state, "health_state", None)
    stop_watchdog = threading.Event()

    async def _heartbeat_loop() -> None:
        while True:
            if state is not None:
                state.touch()
            await asyncio.sleep(heartbeat_interval_seconds)

    def _watchdog() -> None:
        if state is None:
            return
        check_interval = max(0.1, min(5.0, state.stale_after_seconds / 4))
        while not stop_watchdog.wait(check_interval):
            if state.stale:
                logger.critical("event-loop heartbeat stale for %.1fs; exiting", state.stale_after_seconds)
                watchdog_exit(1)
                return

    watchdog_thread = threading.Thread(target=_watchdog, name=f"{health_app.title}-watchdog", daemon=True)
    watchdog_thread.start()

    worker_task = asyncio.ensure_future(worker_coro)
    server_task = asyncio.ensure_future(server.serve())
    heartbeat_task = asyncio.ensure_future(_heartbeat_loop())

    done, pending = await asyncio.wait(
        [worker_task, server_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    stop_watchdog.set()
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass

    for task in pending:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: S110
            pass

    for task in done:
        exc = task.exception()
        if exc is not None:
            raise exc
