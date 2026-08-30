"""Tests for shared/metrics.py — Prometheus metrics registry."""

from __future__ import annotations

from pixav.shared.metrics import (
    get_metrics_output,
    queue_depth,
    record_task_failed,
    record_task_processed,
    record_task_retried,
    set_queue_depth,
    tasks_failed,
    tasks_processed,
    tasks_retried,
)


class TestPrometheusMetrics:
    def test_tasks_processed_counter_increments(self) -> None:
        before = tasks_processed.labels(module="test_module")._value.get()
        tasks_processed.labels(module="test_module").inc()
        after = tasks_processed.labels(module="test_module")._value.get()
        assert after == before + 1

    def test_tasks_failed_counter_increments(self) -> None:
        before = tasks_failed.labels(module="test_module")._value.get()
        tasks_failed.labels(module="test_module").inc()
        after = tasks_failed.labels(module="test_module")._value.get()
        assert after == before + 1

    def test_tasks_retried_counter_increments(self) -> None:
        before = tasks_retried.labels(module="test_module")._value.get()
        tasks_retried.labels(module="test_module").inc()
        after = tasks_retried.labels(module="test_module")._value.get()
        assert after == before + 1

    def test_queue_depth_gauge_sets_value(self) -> None:
        queue_depth.labels(queue_name="pixav:download").set(42)
        assert queue_depth.labels(queue_name="pixav:download")._value.get() == 42

    def test_get_metrics_output_returns_bytes(self) -> None:
        output = get_metrics_output()
        assert isinstance(output, bytes)
        assert len(output) > 0

    def test_get_metrics_output_contains_counter_names(self) -> None:
        tasks_processed.labels(module="output_test").inc()
        output = get_metrics_output().decode("utf-8")
        assert "pixav_tasks_processed_total" in output
        assert "pixav_tasks_failed_total" in output
        assert "pixav_queue_depth" in output


class TestMetricsHelpers:
    """The ``record_*`` helpers are the API workers are expected to call."""

    def test_record_task_processed_increments(self) -> None:
        before = tasks_processed.labels(module="helper_module")._value.get()
        record_task_processed("helper_module")
        assert tasks_processed.labels(module="helper_module")._value.get() == before + 1

    def test_record_task_processed_accepts_count(self) -> None:
        before = tasks_processed.labels(module="helper_module")._value.get()
        record_task_processed("helper_module", 5)
        assert tasks_processed.labels(module="helper_module")._value.get() == before + 5

    def test_record_task_failed_increments(self) -> None:
        before = tasks_failed.labels(module="helper_module")._value.get()
        record_task_failed("helper_module")
        assert tasks_failed.labels(module="helper_module")._value.get() == before + 1

    def test_record_task_retried_increments(self) -> None:
        before = tasks_retried.labels(module="helper_module")._value.get()
        record_task_retried("helper_module")
        assert tasks_retried.labels(module="helper_module")._value.get() == before + 1

    def test_set_queue_depth_publishes_gauge(self) -> None:
        set_queue_depth("pixav:helper", 7)
        assert queue_depth.labels(queue_name="pixav:helper")._value.get() == 7

    def test_recorded_values_appear_as_samples(self) -> None:
        record_task_processed("sample_module")
        set_queue_depth("pixav:sample", 3)
        output = get_metrics_output().decode("utf-8")
        samples = [line for line in output.splitlines() if line and not line.startswith("#")]
        assert any('pixav_tasks_processed_total{module="sample_module"}' in line for line in samples)
        assert any('pixav_queue_depth{queue_name="pixav:sample"}' in line for line in samples)
