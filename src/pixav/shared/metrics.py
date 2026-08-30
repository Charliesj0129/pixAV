"""Shared Prometheus metrics registry for pixAV pipeline workers.

Each worker exposes /metrics on its health port.  All counters/gauges are
module-level singletons on a dedicated :class:`CollectorRegistry` so they
accumulate across the process lifetime without pulling in prometheus_client's
default process/GC collectors.

Workers must not touch the metric objects directly; use the ``record_*`` /
``set_queue_depth`` helpers below so instrumentation stays consistent and
label names cannot drift.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

# Dedicated registry: only pixAV pipeline series are exported.
_REGISTRY = CollectorRegistry()

# ── Task counters ────────────────────────────────────────────────────────────

tasks_processed = Counter(
    "pixav_tasks_processed_total",
    "Total tasks successfully processed",
    ["module"],
    registry=_REGISTRY,
)

tasks_failed = Counter(
    "pixav_tasks_failed_total",
    "Total tasks that failed (permanent failure or DLQ)",
    ["module"],
    registry=_REGISTRY,
)

tasks_retried = Counter(
    "pixav_tasks_retried_total",
    "Total tasks requeued for retry",
    ["module"],
    registry=_REGISTRY,
)

# ── Queue depth gauges ───────────────────────────────────────────────────────

queue_depth = Gauge(
    "pixav_queue_depth",
    "Current number of items in the Redis queue",
    ["queue_name"],
    registry=_REGISTRY,
)


# ── Instrumentation helpers ──────────────────────────────────────────────────


def record_task_processed(module: str, count: int = 1) -> None:
    """Count tasks that reached a successful terminal state in ``module``."""
    tasks_processed.labels(module=module).inc(count)


def record_task_failed(module: str, count: int = 1) -> None:
    """Count tasks that failed permanently (or were sent to a DLQ)."""
    tasks_failed.labels(module=module).inc(count)


def record_task_retried(module: str, count: int = 1) -> None:
    """Count tasks that were requeued for another attempt."""
    tasks_retried.labels(module=module).inc(count)


def set_queue_depth(queue_name: str, depth: int) -> None:
    """Publish the current depth (queued + in-flight) of a Redis queue."""
    queue_depth.labels(queue_name=queue_name).set(depth)


def get_metrics_output() -> bytes:
    """Return Prometheus text-format metrics payload."""
    return generate_latest(_REGISTRY)
