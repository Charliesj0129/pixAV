"""Tests for Sehuatang watermark detection."""

from __future__ import annotations

import pytest

from pixav.shared.watermark import (
    KNOWN_WATERMARK,
    decode_xor_watermark,
    is_known_watermark,
    is_watermark_info_hash,
)

# Observed live in the production qBittorrent client on 2026-08-30. Each is the
# same address under a different XOR key, which is why a literal blocklist of
# hashes cannot work.
LIVE_WATERMARKS = (
    "5c2f3934293d283d323b1c3b313d3530723f3331",
    "5e2d3b362b3f2a3f30391e39333f3732703d3133",
    "a5d6c0cdd0c4d1c4cbc2e5c2c8c4ccc98bc6cac8",
)

REAL_INFO_HASH = "3990382c46c6f5b6a1c0d5b9e8f2a4c7d1e0b3a9"


class TestDecoder:
    @pytest.mark.parametrize("encoded", LIVE_WATERMARKS)
    def test_decodes_every_observed_variant(self, encoded: str) -> None:
        assert decode_xor_watermark(encoded) == KNOWN_WATERMARK

    def test_rejects_non_hex(self) -> None:
        assert decode_xor_watermark("not-a-hash") is None

    def test_rejects_wrong_length(self) -> None:
        assert decode_xor_watermark("abcd") is None


class TestKnownWatermark:
    """Narrow predicate: gates destructive scripts, so it matches only the real thing."""

    @pytest.mark.parametrize("encoded", LIVE_WATERMARKS)
    def test_matches_the_confirmed_address(self, encoded: str) -> None:
        assert is_known_watermark(encoded)

    def test_does_not_match_other_printable_payloads(self) -> None:
        # Some other watermark text: real, but not the address we delete rows for.
        encoded = _encode("different@example.t")
        assert not is_known_watermark(encoded)

    def test_does_not_match_a_real_info_hash(self) -> None:
        assert not is_known_watermark(REAL_INFO_HASH)


class TestBoundaryGuard:
    """Wide predicate: gates the torrent client, where a false positive is cheap."""

    @pytest.mark.parametrize("encoded", LIVE_WATERMARKS)
    def test_rejects_the_confirmed_address(self, encoded: str) -> None:
        assert is_watermark_info_hash(encoded)

    def test_rejects_watermark_variants_the_narrow_rule_would_miss(self) -> None:
        assert is_watermark_info_hash(_encode("different@example.t"))

    def test_accepts_a_real_info_hash(self) -> None:
        assert not is_watermark_info_hash(REAL_INFO_HASH)

    def test_accepts_hashes_decoding_to_control_bytes(self) -> None:
        # 20 zero bytes decode to 19 NULs: binary, not a watermark.
        assert not is_watermark_info_hash("00" * 20)


def _encode(text: str, key: int = 0x42) -> str:
    """Build a watermark hash the way Sehuatang does: key byte, then XORed text."""
    assert len(text) == 19
    return bytes([key] + [ord(char) ^ key for char in text]).hex()
