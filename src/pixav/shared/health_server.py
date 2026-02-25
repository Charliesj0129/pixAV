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
from collections.abc import Coroutine
from typing import Any

import uvicorn
from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def run_with_health(
    *,
    worker_coro: Coroutine[Any, Any, Any],
    health_app: FastAPI,
    host: str = "0.0.0.0",  # noqa: S104
    port: int = 8001,
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

    worker_task = asyncio.ensure_future(worker_coro)
    server_task = asyncio.ensure_future(server.serve())

    done, pending = await asyncio.wait(
        [worker_task, server_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

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
