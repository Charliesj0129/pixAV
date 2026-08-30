"""Tests for worker configuration parsing and execution logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pixav.config import Settings
from pixav.sht_probe.worker import run_once


@pytest.mark.asyncio
async def test_worker_parses_tagged_seeds() -> None:
    """Verify that worker parses 'URL|tag1+tag2' format and calls service."""
    settings = Settings(
        crawl_seed_urls="http://site1.com|tag1, http://site2.com|tagA+tagB, http://site3.com",
        crawl_queries="",
        # Disable external services
        flaresolverr_url="",
        jackett_url="",
        jackett_api_key="",
    )

    with (
        patch("pixav.sht_probe.worker.create_pool", new_callable=AsyncMock),
        patch("pixav.sht_probe.worker.create_redis", new_callable=AsyncMock),
        patch("pixav.sht_probe.worker.ShtProbeService") as mock_service_class,
    ):
        mock_service = mock_service_class.return_value
        mock_service.run_crawl = AsyncMock(return_value=[])
        mock_service._crawler = True  # trick the check

        await run_once(settings)

        # distinct calls expected
        # 1. http://site1.com with tags=["tag1"]
        # 2. http://site2.com with tags=["tagA", "tagB"]
        # 3. http://site3.com with tags=[]

        calls = mock_service.run_crawl.call_args_list
        assert len(calls) == 3

        # Check call 1
        args1, kwargs1 = calls[0]
        assert args1[0] == "http://site1.com"
        assert kwargs1["tags"] == ["tag1"]

        # Check call 2
        args2, kwargs2 = calls[1]
        assert args2[0] == "http://site2.com"
        assert kwargs2["tags"] == ["tagA", "tagB"]

        # Check call 3
        args3, kwargs3 = calls[2]
        assert args3[0] == "http://site3.com"
        assert kwargs3["tags"] == []


@pytest.mark.asyncio
async def test_worker_seeds_cookies_into_both_crawlers(tmp_path) -> None:
    """Sehuatang needs the session too, not just the generic crawler.

    An unseeded SehuatangCrawler browses as a guest: the site answers with the
    age-gate and, once that is cleared, a guest board page holding almost no
    thread links -- so the cycle reports success while discovering nothing.
    """
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "www.sehuatang.org\tFALSE\t/\tTRUE\t0\t_safe\tSAFEVAL\n"
        "www.sehuatang.org\tFALSE\t/\tTRUE\t0\tcPNj_2132_auth\tAUTHVAL\n",
        encoding="utf-8",
    )
    settings = Settings(
        crawl_seed_urls="",
        crawl_queries="",
        crawl_cookie_header="",
        crawl_cookie_file=str(cookie_file),
        flaresolverr_url="http://flaresolverr:8191",
        jackett_url="",
        jackett_api_key="",
    )

    with (
        patch("pixav.sht_probe.worker.create_pool", new_callable=AsyncMock),
        patch("pixav.sht_probe.worker.create_redis", new_callable=AsyncMock),
        patch("pixav.sht_probe.worker.ShtProbeService"),
        patch("pixav.sht_probe.worker.HttpxCrawler") as mock_httpx_crawler,
        patch("pixav.sht_probe.worker.SehuatangCrawler") as mock_sehuatang_crawler,
    ):
        await run_once(settings)

    expected = {"_safe": "SAFEVAL", "cPNj_2132_auth": "AUTHVAL"}
    mock_httpx_crawler.return_value.seed_cookies.assert_called_once_with(expected)
    mock_sehuatang_crawler.return_value.seed_cookies.assert_called_once_with(expected)
