"""Tests for worker configuration parsing and execution logic."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from pixav.config import Settings
from pixav.sht_probe.models import CrawlResult
from pixav.sht_probe.worker import _seed_crawler_cookies, run_loop, run_once


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


def test_missing_cookie_file_records_distinct_metric(tmp_path) -> None:
    settings = Settings(crawl_cookie_file=str(tmp_path / "missing.txt"))

    with patch("pixav.sht_probe.worker.record_crawl_cookie_error") as record:
        with pytest.raises(FileNotFoundError):
            _seed_crawler_cookies(settings)

    record.assert_called_once_with("missing")


def test_invalid_cookie_file_records_distinct_metric(tmp_path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("not a cookie", encoding="utf-8")
    settings = Settings(crawl_cookie_file=str(cookie_file))

    with patch("pixav.sht_probe.worker.record_crawl_cookie_error") as record:
        with pytest.raises(ValueError):
            _seed_crawler_cookies(settings)

    record.assert_called_once_with("invalid")


@pytest.mark.asyncio
async def test_crawl_cycle_timeout_records_distinct_metric() -> None:
    settings = Settings(crawl_interval_seconds=1)

    async def timeout_once(awaitable, *, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    async def stop_after_cycle(_seconds):
        raise asyncio.CancelledError

    with (
        patch("pixav.sht_probe.worker.asyncio.wait_for", side_effect=timeout_once),
        patch("pixav.sht_probe.worker.asyncio.sleep", side_effect=stop_after_cycle),
        patch("pixav.sht_probe.worker.record_crawl_cycle_timeout") as record_timeout,
        patch("pixav.sht_probe.worker.record_task_failed"),
        patch("pixav.sht_probe.worker.set_crawl_interval") as set_interval,
    ):
        with pytest.raises(asyncio.CancelledError):
            await run_loop(settings)

    set_interval.assert_called_once_with(1)
    record_timeout.assert_called_once_with()


def _empty_cycle_settings() -> Settings:
    return Settings(
        crawl_seed_urls="http://site1.com",
        crawl_queries="",
        flaresolverr_url="",
        jackett_url="",
        jackett_api_key="",
        crawl_empty_cycles_key="pixav:crawl:empty_cycles",
    )


@pytest.mark.asyncio
async def test_empty_crawl_increments_persisted_counter() -> None:
    """The empty-cycle streak lives in Redis so a worker restart cannot reset it."""
    redis = AsyncMock()
    # Redis already holds two prior empty cycles from before this process started.
    redis.incr.return_value = 3

    with (
        patch("pixav.sht_probe.worker.create_pool", new_callable=AsyncMock),
        patch("pixav.sht_probe.worker.create_redis", AsyncMock(return_value=redis)),
        patch("pixav.sht_probe.worker.ShtProbeService") as mock_service_class,
        patch("pixav.sht_probe.worker.set_crawl_state") as set_state,
    ):
        mock_service = mock_service_class.return_value
        mock_service.run_crawl = AsyncMock(return_value=CrawlResult([], extracted_magnets=0))

        result = await run_once(_empty_cycle_settings())

    assert result.extracted_magnets == 0
    redis.incr.assert_awaited_once_with("pixav:crawl:empty_cycles")
    redis.set.assert_not_awaited()
    assert set_state.call_args.kwargs["empty_cycles"] == 3


@pytest.mark.asyncio
async def test_extracted_magnets_reset_the_counter_even_with_no_new_rows() -> None:
    """Emptiness means 'extracted zero magnets', not 'inserted zero new videos'."""
    redis = AsyncMock()

    with (
        patch("pixav.sht_probe.worker.create_pool", new_callable=AsyncMock),
        patch("pixav.sht_probe.worker.create_redis", AsyncMock(return_value=redis)),
        patch("pixav.sht_probe.worker.ShtProbeService") as mock_service_class,
        patch("pixav.sht_probe.worker.set_crawl_state") as set_state,
    ):
        mock_service = mock_service_class.return_value
        # Every magnet was a duplicate: nothing new persisted, but the crawl worked.
        mock_service.run_crawl = AsyncMock(return_value=CrawlResult([], extracted_magnets=12))

        await run_once(_empty_cycle_settings())

    redis.set.assert_awaited_once_with("pixav:crawl:empty_cycles", 0)
    redis.incr.assert_not_awaited()
    assert set_state.call_args.kwargs["empty_cycles"] == 0
