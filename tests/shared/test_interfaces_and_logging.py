"""Sensitive HTTP dependency logs must redact URLs and exception details."""

from __future__ import annotations

import logging

from pixav.shared.logging import install_http_url_log_redaction


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
