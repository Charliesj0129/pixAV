"""Tests for QualityScorer and ShtProbeService dedup/scoring logic."""

import uuid
from unittest.mock import AsyncMock

import pytest

from pixav.shared.models import Video
from pixav.sht_probe.scoring import QualityScorer
from pixav.sht_probe.service import ShtProbeService

# ---------------------------------------------------------------------------
# QualityScorer unit tests
# ---------------------------------------------------------------------------


class TestQualityScorer:
    def setup_method(self) -> None:
        self.scorer = QualityScorer()

    def test_extract_info_hash(self) -> None:
        magnet = "magnet:?xt=urn:btih:ABCDEF1234567890&dn=Test"
        assert self.scorer.extract_info_hash(magnet) == "abcdef1234567890"

    def test_extract_info_hash_invalid(self) -> None:
        assert self.scorer.extract_info_hash("not-a-magnet") is None

    def test_resolution_ranking(self) -> None:
        s4k = self.scorer.score("Movie 2160p HEVC")
        s1080 = self.scorer.score("Movie 1080p x264")
        s720 = self.scorer.score("Movie 720p")
        assert s4k > s1080 > s720

    def test_seeders_bonus(self) -> None:
        base = self.scorer.score("Movie 1080p")
        with_seeders = self.scorer.score("Movie 1080p", seeders=50)
        assert with_seeders > base

    def test_seeders_capped(self) -> None:
        many = self.scorer.score("Movie", seeders=500)
        extreme = self.scorer.score("Movie", seeders=5000)
        # both should get same seeder bonus (capped at 50)
        assert many == extreme

    def test_size_bonus_sweet_spot(self) -> None:
        gb4 = int(4 * 1024**3)
        no_size = self.scorer.score("Movie 1080p")
        with_size = self.scorer.score("Movie 1080p", size_bytes=gb4)
        assert with_size == no_size + 30

    def test_size_penalty_tiny(self) -> None:
        tiny = int(0.05 * 1024**3)  # 50 MB
        base = self.scorer.score("Movie 1080p")
        small = self.scorer.score("Movie 1080p", size_bytes=tiny)
        assert small < base

    def test_size_penalty_huge(self) -> None:
        huge = int(25 * 1024**3)
        base = self.scorer.score("Movie 1080p")
        big = self.scorer.score("Movie 1080p", size_bytes=huge)
        assert big < base

    def test_cam_penalty(self) -> None:
        assert self.scorer.score("Bad Movie CAM") < 0

    def test_size_unknown_no_effect(self) -> None:
        """size_bytes=0 should not alter score."""
        assert self.scorer.score("Movie") == self.scorer.score("Movie", size_bytes=0)


# ---------------------------------------------------------------------------
# ShtProbeService dedup/scoring integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_by_info_hash() -> None:
    repo = AsyncMock()
    queue = AsyncMock()

    service = ShtProbeService(video_repo=repo, queue=queue)

    existing_video = Video(
        id=uuid.uuid4(),
        title="Existing",
        magnet_uri="magnet:?xt=urn:btih:AAAA",
        info_hash="aaaa",
        quality_score=10,
    )

    async def fake_find(info_hash: str) -> Video | None:
        return existing_video if info_hash == "aaaa" else None

    repo.find_by_info_hash = AsyncMock(side_effect=fake_find)
    repo.find_by_magnet = AsyncMock(return_value=None)

    magnets = [
        "magnet:?xt=urn:btih:AAAA&dn=Dup",  # exists
        "magnet:?xt=urn:btih:BBBB&dn=New",  # new
    ]

    new = await service._persist_new(magnets)
    assert len(new) == 1
    assert repo.insert.call_count == 1

    inserted = repo.insert.call_args[0][0]
    assert inserted.info_hash == "bbbb"
    assert inserted.quality_score >= 0


@pytest.mark.asyncio
async def test_quality_gate_filters_low_score() -> None:
    repo = AsyncMock()
    queue = AsyncMock()
    repo.find_by_info_hash = AsyncMock(return_value=None)
    repo.find_by_magnet = AsyncMock(return_value=None)

    service = ShtProbeService(video_repo=repo, queue=queue, min_quality_score=100)

    # low quality — no resolution, no seeders
    magnets = ["magnet:?xt=urn:btih:CCCC&dn=Untitled"]
    new = await service._persist_new(magnets)
    assert len(new) == 0
    assert repo.insert.call_count == 0


@pytest.mark.asyncio
async def test_seeders_from_results() -> None:
    repo = AsyncMock()
    queue = AsyncMock()
    repo.find_by_info_hash = AsyncMock(return_value=None)
    repo.find_by_magnet = AsyncMock(return_value=None)

    service = ShtProbeService(video_repo=repo, queue=queue)

    magnets = ["magnet:?xt=urn:btih:DDDD&dn=Movie+1080p"]
    results = [
        {
            "magnet_uri": magnets[0],
            "title": "Movie 1080p HEVC",
            "seeders": 100,
            "size": int(3 * 1024**3),
        }
    ]

    new = await service._persist_new(magnets, results=results)
    assert len(new) == 1

    inserted = repo.insert.call_args[0][0]
    # 1080p(50) + hevc(40) + seeders_capped(50) + size_sweet(30) = 170
    assert inserted.quality_score == 170
