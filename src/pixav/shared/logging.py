"""Structured logging setup using structlog."""

from __future__ import annotations

import logging
import re
import sys
import traceback

import structlog

_HTTP_URL_RE = re.compile(r"https?://[^\s]+", flags=re.IGNORECASE)


class _HttpUrlRedactionFilter(logging.Filter):
    """Remove complete HTTP URLs from dependency logs before propagation.

    Google Photos share, redirect and CDN URLs all contain bearer-like tokens.
    httpx logs the complete request URL at INFO by default, so application-level
    exception redaction alone is not sufficient.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = _HTTP_URL_RE.sub("<redacted-http-url>", message)
        record.args = ()

        if record.exc_info is not None:
            exception_text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = _HTTP_URL_RE.sub("<redacted-http-url>", exception_text)
            record.exc_info = None
        return True


def install_http_url_log_redaction() -> None:
    """Install an idempotent URL filter on httpx/httpcore loggers."""
    for name in ("httpx", "httpcore"):
        dependency_logger = logging.getLogger(name)
        if not any(isinstance(item, _HttpUrlRedactionFilter) for item in dependency_logger.filters):
            dependency_logger.addFilter(_HttpUrlRedactionFilter())


def setup_logging(*, level: int = logging.INFO, json_output: bool = True) -> None:
    """Configure structlog with JSON output for production or pretty-print for dev."""
    install_http_url_log_redaction()
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )
