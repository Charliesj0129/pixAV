"""Coverage tests for protocol interface modules and logging setup."""

from __future__ import annotations

import logging

from pixav.maxwell_core.interfaces import BackpressureMonitor, TaskDispatcher, TaskScheduler
from pixav.shared.logging import install_http_url_log_redaction, setup_logging
from pixav.sht_probe.interfaces import (
    ContentCrawler,
    FlareSolverSession,
    IndexerAdapter,
    JackettSearcher,
    MagnetExtractor,
)


class _SchedulerImpl:
    async def next_account(self) -> str:
        return "acct-1"


class _DispatcherImpl:
    async def dispatch(self, task_id: str, queue_name: str) -> None:
        return None


class _MonitorImpl:
    async def check_pressure(self, queue_name: str) -> bool:
        return True


class _CrawlerImpl:
    async def crawl(self, url: str) -> list[str]:
        return [url]


class _ExtractorImpl:
    async def extract(self, page_url: str) -> list[str]:
        return [f"magnet:{page_url}"]


class _JackettImpl:
    async def search(self, query: str, *, limit: int = 50) -> list[dict[str, object]]:
        return [{"title": query, "magnet_uri": None, "size": 0, "seeders": 0}]


class _FlareSolverImpl:
    async def get_html(
        self,
        url: str,
        *,
        timeout: int = 60,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        return "<html></html>", {}


def test_maxwell_protocol_runtime_checks() -> None:
    assert isinstance(_SchedulerImpl(), TaskScheduler)
    assert isinstance(_DispatcherImpl(), TaskDispatcher)
    assert isinstance(_MonitorImpl(), BackpressureMonitor)


def test_sht_protocol_runtime_checks() -> None:
    assert isinstance(_CrawlerImpl(), ContentCrawler)
    assert isinstance(_ExtractorImpl(), MagnetExtractor)
    assert isinstance(_JackettImpl(), JackettSearcher)
    assert isinstance(_JackettImpl(), IndexerAdapter)
    assert isinstance(_FlareSolverImpl(), FlareSolverSession)


def test_setup_logging_console_and_json() -> None:
    setup_logging(level=logging.DEBUG, json_output=False)
    setup_logging(level=logging.INFO, json_output=True)


def test_http_dependency_logs_redact_urls_and_exception_tracebacks(caplog) -> None:
    token = "synthetic-sensitive-token"
    secret_url = f"https://photos.app.goo.gl/{token}?signed=yes"
    install_http_url_log_redaction()
    install_http_url_log_redaction()

    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: GET "%s" 200 OK', secret_url)
        try:
            raise RuntimeError(f"transport rejected {secret_url}")
        except RuntimeError:
            logging.getLogger("httpx").error("request failed", exc_info=True)

    assert token not in caplog.text
    assert secret_url not in caplog.text
    assert "<redacted-http-url>" in caplog.text
