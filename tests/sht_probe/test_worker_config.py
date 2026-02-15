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
