"""Tests for shared/metrics.py — Prometheus metrics registry."""

from __future__ import annotations

from pixav.shared.metrics import get_metrics_output, queue_depth, tasks_failed, tasks_processed, tasks_retried


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
