"""Operator entry points must default to no effects and check exact identity."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from pixav.strm_resolver import manage


@pytest.mark.parametrize("action", ["prepare", "publish", "evict-cache", "reconcile"])
@pytest.mark.parametrize("mode", ["dry", "apply", "wrong-instance", "wrong-database"])
async def test_operator_actions_check_identity_before_effects(monkeypatch, tmp_path, action, mode):
    video_id = uuid4()
    pool = AsyncMock()
    pool.fetchval.return_value = "isolated" if mode != "wrong-database" else "other"
    pool.fetch.return_value = [{"video_id": video_id}]
    monkeypatch.setattr(manage.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(
        manage, "database_identity", AsyncMock(return_value="expected" if mode != "wrong-instance" else "other")
    )
    monkeypatch.setattr(
        manage,
        "get_settings",
        lambda: SimpleNamespace(
            dsn="synthetic",
            library_projection_dir=str(tmp_path / "library"),
            library_artwork_dir=str(tmp_path / "art"),
            playback_public_url="https://fixture.invalid",
        ),
    )
    playback, projection = AsyncMock(), AsyncMock()
    make_playback, make_projection = Mock(return_value=playback), Mock(return_value=projection)
    monkeypatch.setattr(manage.PlaybackService, "configured", make_playback)
    monkeypatch.setattr(manage, "LibraryProjection", make_projection)
    args = SimpleNamespace(
        action=action, apply=mode != "dry", db_identity="expected", database="isolated", video_id=video_id, poster=None
    )
    if mode.startswith("wrong"):
        with pytest.raises(ValueError, match="identity mismatch"):
            await manage.run(args)
    else:
        result = await manage.run(args)
        assert result["apply"] == (mode == "apply")
    if mode != "apply":
        make_playback.assert_not_called()
        make_projection.assert_not_called()
    elif action == "prepare":
        playback.prepare.assert_awaited_once_with(video_id)
    elif action == "publish":
        projection.publish.assert_awaited_once_with(video_id, None)
    elif action == "evict-cache":
        playback.evict.assert_awaited_once_with(video_id)
    else:
        projection.withdraw_invalid.assert_awaited_once_with(video_id)
        assert pool.fetch.call_args.args[1] == video_id
        projection.publish.assert_awaited_once_with(video_id)
    pool.close.assert_awaited_once()
