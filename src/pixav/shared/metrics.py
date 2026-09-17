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

from collections.abc import Iterable, Mapping

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

dlq_depth = Gauge(
    "pixav_dlq_depth",
    "Unique terminal tasks in the bounded Redis DLQ index",
    ["stage"],
    registry=_REGISTRY,
)

dlq_terminal = Counter(
    "pixav_dlq_terminal_total",
    "Tasks entering a terminal dead-letter state",
    ["stage"],
    registry=_REGISTRY,
)

cleanup_failures = Counter(
    "pixav_local_cleanup_failures_total",
    "Local media cleanup failures",
    registry=_REGISTRY,
)

# ── Remote storage ───────────────────────────────────────────────────────────

# Waiting for quota is an expected, recoverable state. Reporting it as a generic
# failure would hide a healthy pipeline behind an alert nobody should act on.
executions_waiting_quota = Gauge(
    "pixav_executions_waiting_quota",
    "Executions currently waiting for upload quota to reset",
    registry=_REGISTRY,
)

# Waiting for a source is also expected and recoverable: the pipeline is
# correctly refusing to invent a download, which is not a failure to page on.
executions_source_unavailable = Gauge(
    "pixav_executions_source_unavailable",
    "Executions waiting for any eligible source candidate to recover",
    registry=_REGISTRY,
)

executions_user_action_required = Gauge(
    "pixav_executions_user_action_required",
    "Executions stopped for an operator decision, such as a login challenge",
    registry=_REGISTRY,
)

# Terminal executions are never replayed automatically. A flat series is the
# evidence for that; a climbing one means something is replaying them.
executions_terminal = Gauge(
    "pixav_executions_terminal",
    "Executions in a terminal state, by final failure classification",
    ["failure_class"],
    registry=_REGISTRY,
)

# An upload that never happened and an upload whose bytes failed to come back
# are different incidents with different responses, so they are counted apart.
remote_upload_failures = Counter(
    "pixav_remote_upload_failures_total",
    "Uploads that did not create a remote asset",
    ["reason"],
    registry=_REGISTRY,
)

remote_verification_failures = Counter(
    "pixav_remote_verification_failures_total",
    "Remote assets whose cold read-back failed its integrity policy",
    ["reason"],
    registry=_REGISTRY,
)

remote_assets_durable = Counter(
    "pixav_remote_assets_durable_total",
    "Remote assets promoted to DURABLE after a verified cold read-back",
    registry=_REGISTRY,
)

# Withdrawing a durability claim is the most serious thing this pipeline can
# report about itself, so it is counted apart from an ordinary verification
# failure: the latter means "not proven yet", this means "was proven, is not".
remote_assets_invalidated = Counter(
    "pixav_remote_assets_invalidated_total",
    "Durable remote assets withdrawn after a failed re-read, by confirmed reason",
    ["reason"],
    registry=_REGISTRY,
)

# A remote copy nobody has looked at since it was stored is an assumption. This
# is how long the backlog of unexamined assumptions has grown.
remote_assets_due_reverification = Gauge(
    "pixav_remote_assets_due_reverification",
    "Durable remote assets overdue for an independent cold re-read",
    registry=_REGISTRY,
)

# A provider row this pipeline could not parse is discarded, and without this
# the discard is invisible: an adapter returning nothing usable and a provider
# with nothing to offer both look like a quiet cycle.
source_adapter_errors = Counter(
    "pixav_source_adapter_errors_total",
    "Provider payloads rejected at the adapter boundary, by refusal reason",
    ["reason"],
    registry=_REGISTRY,
)

cleanup_rejections = Counter(
    "pixav_local_cleanup_rejections_total",
    "Cleanup evaluations refused, by the guarantee that was missing",
    ["condition"],
    registry=_REGISTRY,
)

download_paused = Gauge(
    "pixav_download_paused",
    "Whether the persistent disk-safety download pause is latched",
    registry=_REGISTRY,
)

disk_free_bytes = Gauge(
    "pixav_disk_free_bytes",
    "Free bytes on the download filesystem",
    ["path"],
    registry=_REGISTRY,
)

disk_free_percent = Gauge(
    "pixav_disk_free_percent",
    "Free percent on the download filesystem",
    ["path"],
    registry=_REGISTRY,
)

worker_heartbeat = Gauge(
    "pixav_worker_heartbeat_timestamp_seconds",
    "Last event-loop heartbeat as Unix time",
    ["module"],
    registry=_REGISTRY,
)

worker_ready = Gauge(
    "pixav_worker_ready",
    "Whether worker dependencies and main loop are ready",
    ["module"],
    registry=_REGISTRY,
)

worker_start_time = Gauge(
    "pixav_worker_start_time_seconds",
    "Worker process start time",
    ["module"],
    registry=_REGISTRY,
)

crawl_empty_cycles = Gauge(
    "pixav_crawl_consecutive_empty_cycles",
    "Consecutive crawl cycles that extracted zero magnets",
    ["module"],
    registry=_REGISTRY,
)

crawl_last_completed = Gauge(
    "pixav_crawl_last_completed_timestamp_seconds",
    "Last completed crawl cycle as Unix time",
    ["module"],
    registry=_REGISTRY,
)

crawl_age_gate = Gauge(
    "pixav_crawl_age_gate_active",
    "Whether the most recent crawl still encountered an unresolved age gate",
    ["module"],
    registry=_REGISTRY,
)

crawl_watermark_rejected = Counter(
    "pixav_crawl_watermark_rejected_total",
    "Bare 40-hex strings rejected as Sehuatang watermarks rather than info hashes",
    ["module"],
    registry=_REGISTRY,
)

crawl_untitled_rejected = Counter(
    "pixav_crawl_untitled_rejected_total",
    "Sehuatang candidates rejected because no useful title was available",
    ["module"],
    registry=_REGISTRY,
)

crawl_interval = Gauge(
    "pixav_crawl_interval_seconds",
    "Configured delay between crawl cycles",
    ["module"],
    registry=_REGISTRY,
)

crawl_cookie_errors = Counter(
    "pixav_crawl_cookie_errors_total",
    "Crawler cookie configuration failures",
    ["module", "reason"],
    registry=_REGISTRY,
)

crawl_cycle_timeouts = Counter(
    "pixav_crawl_cycle_timeouts_total",
    "Crawler cycles terminated by the interval plus grace timeout",
    ["module"],
    registry=_REGISTRY,
)

stash_failures = Counter(
    "pixav_stash_enrichment_failures_total",
    "Best-effort Stash enrichment failures while enabled",
    registry=_REGISTRY,
)

# One series per state with exactly one of them at 1. A single gauge encoding
# states as numbers would make "unknown" and "leaking" adjacent values, and an
# alert that fires on the wrong comparison operator is worse than no alert.
vpn_egress_state = Gauge(
    "pixav_vpn_egress_state",
    "Whether torrent egress is isolated from the host's public address",
    ["state"],
    registry=_REGISTRY,
)

vpn_probe_failures = Counter(
    "pixav_vpn_egress_probe_failures_total",
    "Public IP probes that could not answer, by observing side",
    ["side"],
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


def set_dlq_depth(stage: str, depth: int) -> None:
    dlq_depth.labels(stage=stage).set(depth)


def record_dlq_terminal(stage: str) -> None:
    dlq_terminal.labels(stage=stage).inc()


def set_executions_waiting_quota(count: int) -> None:
    executions_waiting_quota.set(count)


def set_executions_source_unavailable(count: int) -> None:
    executions_source_unavailable.set(count)


def set_executions_user_action_required(count: int) -> None:
    executions_user_action_required.set(count)


def set_executions_terminal(counts: Mapping[str, int]) -> None:
    """Publish one series per classification, clearing the ones that emptied."""
    for labels in list(executions_terminal._metrics):  # noqa: SLF001 - no public clear-by-label API
        if labels[0] not in counts:
            executions_terminal.remove(*labels)
    for failure_class, count in counts.items():
        executions_terminal.labels(failure_class=failure_class).set(count)


def record_remote_upload_failure(reason: str) -> None:
    remote_upload_failures.labels(reason=reason).inc()


def record_remote_verification_failure(reason: str) -> None:
    remote_verification_failures.labels(reason=reason).inc()


def record_remote_asset_durable() -> None:
    remote_assets_durable.inc()


def record_remote_asset_invalidated(reason: str) -> None:
    remote_assets_invalidated.labels(reason=reason).inc()


def set_remote_assets_due_reverification(count: int) -> None:
    remote_assets_due_reverification.set(count)


def record_source_adapter_error(reason: str = "INVALID_PROVIDER_PAYLOAD") -> None:
    """Count one provider row the adapter refused to turn into a candidate.

    The label is the adapter's own bounded refusal vocabulary, never the
    provider name: that is external input and would be unbounded cardinality.
    """
    source_adapter_errors.labels(reason=reason).inc()


def record_cleanup_rejection(condition: str) -> None:
    cleanup_rejections.labels(condition=condition).inc()


def record_cleanup_failure() -> None:
    cleanup_failures.inc()


def set_download_paused(paused: bool) -> None:
    download_paused.set(1 if paused else 0)


def set_vpn_egress_state(state: str, known_states: Iterable[str]) -> None:
    """Publish the egress verdict, zeroing the states that no longer hold.

    The caller passes the full state set because it owns the vocabulary. Without
    the explicit zeroing, a transition from ``leaking`` back to ``isolated``
    would leave both series at 1 and latch any alert on the leak series forever.
    """
    for known in known_states:
        vpn_egress_state.labels(state=known).set(1 if known == state else 0)


def record_vpn_probe_failure(side: str) -> None:
    vpn_probe_failures.labels(side=side).inc()


def set_disk_free(path: str, free_bytes: int, free_percent: float) -> None:
    disk_free_bytes.labels(path=path).set(free_bytes)
    disk_free_percent.labels(path=path).set(free_percent)


def set_worker_health(module: str, *, ready: bool, heartbeat_timestamp: float, start_timestamp: float) -> None:
    worker_ready.labels(module=module).set(1 if ready else 0)
    worker_heartbeat.labels(module=module).set(heartbeat_timestamp)
    worker_start_time.labels(module=module).set(start_timestamp)


def set_crawl_state(
    *,
    empty_cycles: int,
    completed_timestamp: float,
    age_gate_active: bool,
    module: str = "sht_probe",
) -> None:
    crawl_empty_cycles.labels(module=module).set(empty_cycles)
    crawl_last_completed.labels(module=module).set(completed_timestamp)
    crawl_age_gate.labels(module=module).set(1 if age_gate_active else 0)


def record_untitled_rejected(module: str = "sht_probe") -> None:
    crawl_untitled_rejected.labels(module=module).inc()


def record_watermark_rejected(module: str = "sht_probe") -> None:
    """Count one bare hex string discarded as a watermark.

    Without this the filter is invisible: a cycle that discards every candidate
    and a cycle that found nothing both report ``new=0``.
    """
    crawl_watermark_rejected.labels(module=module).inc()


def set_crawl_interval(seconds: int, module: str = "sht_probe") -> None:
    crawl_interval.labels(module=module).set(max(0, seconds))


def record_crawl_cookie_error(reason: str, module: str = "sht_probe") -> None:
    crawl_cookie_errors.labels(module=module, reason=reason).inc()


def record_crawl_cycle_timeout(module: str = "sht_probe") -> None:
    crawl_cycle_timeouts.labels(module=module).inc()


def record_stash_failure() -> None:
    stash_failures.inc()


def get_metrics_output() -> bytes:
    """Return Prometheus text-format metrics payload."""
    return generate_latest(_REGISTRY)
