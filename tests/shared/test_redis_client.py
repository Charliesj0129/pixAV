"""Tests for shared/redis_client.py — Redis connection factory."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pixav.shared.redis_client import create_redis


class TestCreateRedis:
    async def test_returns_redis_client(self) -> None:
        """create_redis should call aioredis.from_url with the configured URL."""
        settings = MagicMock()
        settings.redis_url = "redis://localhost:6379/0"

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
        settings = MagicMock()
        settings.redis_url = "redis://redis-host:6380/1"

        mock_client = MagicMock()
        with patch("pixav.shared.redis_client.aioredis.from_url", return_value=mock_client) as mock_from_url:
            await create_redis(settings)

        mock_from_url.assert_called_once_with(
            "redis://redis-host:6380/1",
            decode_responses=True,
        )

    async def test_decode_responses_always_true(self) -> None:
        """decode_responses must always be True so we work with str, not bytes."""
        settings = MagicMock()
        settings.redis_url = "redis://localhost:6379/0"

        with patch("pixav.shared.redis_client.aioredis.from_url") as mock_from_url:
            mock_from_url.return_value = MagicMock()
            await create_redis(settings)

        _, kwargs = mock_from_url.call_args
        assert kwargs.get("decode_responses") is True
