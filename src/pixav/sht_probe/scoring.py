"""Quality scoring for discovered media."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


class QualityScorer:
    """rank and filter media based on metadata."""

    # Scoring Weights
    WEIGHTS = {
        "4k": 100.0,
        "1080p": 50.0,
        "720p": 10.0,
        "h265": 40.0,
        "h264": 20.0,
        "seeder_bonus": 0.5,  # points per seeder
    }

    # Blocklist Configuration
    BLOCKED_KEYWORDS = {" vr ", "-vr", "3d", ".iso", ".wmv", ".avi"}
    MAX_SIZE_BYTES = 15 * 1024**3  # 15 GB

    def score(self, title: str, seeders: int = 0, size_bytes: int = 0) -> int:
        """Calculate a quality score for a given release.

        Returns:
            Integer quality score (higher is better).
            Returns -10000 if release is blocked (VR, 3D, ISO, WMV, >15GB).
        """
        norm_title = title.lower()

        # 1. Blocklist (Hard Reject)
        if self._is_blocked(norm_title, size_bytes):
            return -10000

        # 2. Additive Scoring
        score = (
            self._resolution_score(norm_title)
            + self._codec_score(norm_title)
            + self._container_score(norm_title)
            + self._seeder_score(seeders)
            + self._size_score(size_bytes)
            + self._bonus_score(norm_title)
            + self._penalty_score(norm_title)
        )
        return int(score)

    def _is_blocked(self, norm_title: str, size_bytes: int) -> bool:
        # File type blocks
        for keyword in self.BLOCKED_KEYWORDS:
            if keyword in norm_title:
                logger.debug("blocked release '%s': contains '%s'", norm_title, keyword)
                return True

        # Hard size cap
        if size_bytes > self.MAX_SIZE_BYTES:
            logger.debug(
                "blocked release '%s': size %.2f GB > %.2f GB",
                norm_title,
                size_bytes / (1024**3),
                self.MAX_SIZE_BYTES / (1024**3),
            )
            return True

        return False

    def _resolution_score(self, norm_title: str) -> float:
        if "2160p" in norm_title or "4k" in norm_title:
            return self.WEIGHTS["4k"]
        if "1080p" in norm_title:
            return self.WEIGHTS["1080p"]
        if "720p" in norm_title:
            return self.WEIGHTS["720p"]
        return 0.0

    def _codec_score(self, norm_title: str) -> float:
        if "x265" in norm_title or "h265" in norm_title or "hevc" in norm_title:
            return self.WEIGHTS["h265"]
        if "x264" in norm_title or "h264" in norm_title or "avc" in norm_title:
            return self.WEIGHTS["h264"]
        return 0.0

    def _container_score(self, norm_title: str) -> float:
        if ".mp4" in norm_title:
            return 50.0  # Tier S: Direct play
        if ".mkv" in norm_title:
            return 30.0  # Tier A: Remux needed but fast
        return 0.0  # Tier B: No explicit container info

    def _seeder_score(self, seeders: int) -> float:
        return float(min(int(seeders * self.WEIGHTS["seeder_bonus"]), 50))

    def _size_score(self, size_bytes: int) -> float:
        if size_bytes <= 0:
            return 0.0
        size_gb = size_bytes / (1024**3)
        # 1-8 GB sweet spot logic
        if 1.0 <= size_gb <= 8.0:
            return 30.0
        if size_gb < 0.1:
            return -50.0
        return 0.0

    def _bonus_score(self, norm_title: str) -> float:
        bonus = 0.0
        if any(x in norm_title for x in ("中文字幕", "cn sub", "字幕")):
            bonus += 20.0
        if "60fps" in norm_title:
            bonus += 15.0
        return bonus

    def _penalty_score(self, norm_title: str) -> float:
        if "cam" in norm_title or "telesync" in norm_title:
            return -1000.0
        return 0.0

    def extract_info_hash(self, magnet_uri: str) -> str | None:
        """Extract info_hash from magnet URI."""
        match = re.search(r"xt=urn:btih:([a-zA-Z0-9]+)", magnet_uri)
        if match:
            return match.group(1).lower()
        return None
