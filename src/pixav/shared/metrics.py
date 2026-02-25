"""Shared Prometheus metrics registry for pixAV pipeline workers.

Each worker exposes /metrics on its health port.  All counters/gauges are
module-level singletons so they accumulate across the process lifetime.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

# Use the default registry so prometheus_client's built-in metrics are included.
# Workers that need per-module isolation can pass a custom registry.
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


def get_metrics_output() -> bytes:
    """Return Prometheus text-format metrics payload."""
    return generate_latest(_REGISTRY)
