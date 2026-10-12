"""Tests for shared/db.py — asyncpg connection pool factory."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from pixav.shared.db import create_pool, init_connection


class TestInitConnection:
    async def test_registers_pgvector_codec(self) -> None:
        conn = MagicMock()
        with patch("pixav.shared.db.pgvector.asyncpg.register_vector", new_callable=AsyncMock) as mock_register:
            await init_connection(conn)
            mock_register.assert_awaited_once_with(conn)


class TestCreatePool:
    async def test_returns_pool(self) -> None:
        """create_pool should call asyncpg.create_pool with dsn and init hook."""
        settings = MagicMock()
        settings.dsn = "postgresql://pixav:pixav@localhost:5432/pixav"

        mock_pool = MagicMock()

        with patch("pixav.shared.db.asyncpg.create_pool", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_pool
            pool = await create_pool(settings)

        assert pool is mock_pool
        mock_create.assert_awaited_once_with(
            dsn=settings.dsn,
            min_size=2,
            max_size=10,
            init=init_connection,
        )
