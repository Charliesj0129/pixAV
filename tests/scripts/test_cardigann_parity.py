"""Parity is non-vacuous and checks the public domain contract."""

from scripts.cardigann_parity import compare

HASH = "08ada5a7a6183aae1e09d831df6748d566095a10"


def candidate(**changes):
    return dict(
        title="fixture title",
        magnet_uri=f"magnet:?xt=urn:btih:{HASH}",
        source_url="https://www.sehuatang.org/forum.php?mod=viewthread&tid=42",
        size=0,
        seeders=0,
        **changes,
    )


def test_empty_baseline_is_blocked_and_missing_magnet_fails():
    assert compare([], [candidate()])["status"] == "BLOCKED"
    assert compare([candidate()], [dict(candidate(), magnet_uri=None)])["status"] == "FAIL"


def test_normalized_hash_title_source_and_zero_scoring_contract():
    actual = dict(
        candidate(),
        magnet_uri=f"magnet:?xt=urn:btih:{HASH.upper()}&dn=title",
        title="fixture  title",
        source_url="https://www.sehuatang.org/forum.php?mod=viewthread&tid=42&page=2",
        size=None,
        seeders=None,
    )
    assert compare([candidate()], [actual])["status"] == "PASS"
    result = compare([candidate()], [dict(actual, title="wrong", seeders=5)])
    assert result["status"] == "FAIL"
    assert result["field_mismatches"][HASH] == ["title", "seeders"]


def test_known_watermark_does_not_inflate_recall():
    encoded = bytes([0x42, *(ord(c) ^ 0x42 for c in "sehuatang@gmail.com")]).hex()
    watermark = dict(candidate(), magnet_uri=f"magnet:?xt=urn:btih:{encoded}")
    result = compare([candidate()], [candidate(), watermark])
    assert result["status"] == "PASS"
    assert result["observed_valid_count"] == 1
    assert result["actual_watermarks_rejected"] == 1
