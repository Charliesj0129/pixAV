"""Tests for shared/redis_client.py — Redis connection factory."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis.asyncio as aioredis
from pydantic import ValidationError

from pixav.config import Settings
from pixav.shared.redis_client import create_redis


class TestCreateRedis:
    async def test_returns_redis_client(self) -> None:
        """create_redis should call aioredis.from_url with the configured URL."""
        settings = Settings(redis_url="redis://localhost:6379/0")

        mock_client = MagicMock()
        with patch("pixav.shared.redis_client.aioredis.from_url", return_value=mock_client) as mock_from_url:
            client = await create_redis(settings)

        assert client is mock_client
        mock_from_url.assert_called_once_with(
            "redis://localhost:6379/0",
            decode_responses=True,
        )

    async def test_uses_settings_redis_url(self) -> None:
        """Redis URL comes from settings.redis_url."""
        settings = Settings(redis_url="redis://redis-host:6380/1")

        mock_client = MagicMock()
        with patch("pixav.shared.redis_client.aioredis.from_url", return_value=mock_client) as mock_from_url:
            await create_redis(settings)

        mock_from_url.assert_called_once_with(
            "redis://redis-host:6380/1",
            decode_responses=True,
        )

    async def test_decode_responses_always_true(self) -> None:
        """decode_responses must always be True so we work with str, not bytes."""
        settings = Settings(redis_url="redis://localhost:6379/0")

        with patch("pixav.shared.redis_client.aioredis.from_url") as mock_from_url:
            mock_from_url.return_value = MagicMock()
            await create_redis(settings)

        _, kwargs = mock_from_url.call_args
        assert kwargs.get("decode_responses") is True


@pytest.mark.parametrize(
    "url", ["redis://localhost:9/1", "redis://localhost:9/0?db=1", "unix:///tmp/synthetic.sock?db=1"]
)
async def test_database_binding_uses_the_actual_url_parser_without_connecting(url):
    client = await create_redis(Settings(redis_url=url, expected_redis_database=1))
    try:
        assert client.connection_pool.connection_kwargs["db"] == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "url", ["redis://localhost:9/0", "redis://localhost:9/1?db=0", "unix:///tmp/synthetic.sock?db=0"]
)
async def test_wrong_database_is_closed_before_instance_lookup_or_work(url):
    client = aioredis.from_url(url)
    close = AsyncMock(wraps=client.aclose)
    info = AsyncMock(return_value={"run_id": "a" * 40})
    with (
        patch("pixav.shared.redis_client.aioredis.from_url", return_value=client),
        patch.object(client, "aclose", close),
        patch.object(client, "info", info),
    ):
        try:
            with pytest.raises(RuntimeError, match="REDIS_DATABASE_BINDING_CHANGED"):
                await create_redis(Settings(redis_url=url, expected_redis_run_id="a" * 40, expected_redis_database=1))
            info.assert_not_awaited()
            close.assert_awaited_once()
        finally:
            await client.aclose()


def test_negative_database_binding_is_invalid():
    with pytest.raises(ValidationError):
        Settings(expected_redis_database=-1)
