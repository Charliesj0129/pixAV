"""Poster-declared size and resolution, read from real captured Sehuatang HTML."""

from pathlib import Path

import pytest

from pixav.sht_probe.sehuatang import SehuatangExtractor
from scripts.cardigann_spike import sanitized_fixture

FIXTURES = Path("tests/fixtures/sehuatang_20260907")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("【影片容量】：2.27G", int(2.27 * 1024**3)),
        ("【影片容量】: 2.27 GB", int(2.27 * 1024**3)),
        ("容量：4.5 GB", int(4.5 * 1024**3)),
        ("【文件大小】：800M", 800 * 1024**2),
        ("【影片容量】：1.2TB", int(1.2 * 1024**4)),
        ("【影片容量】：512KB", 512 * 1024),
        # No declaration, and an unlabelled number is never treated as one.
        ("【出演女优】：某人", 0),
        ("(119.49 KB, 下载次数: 0)", 0),
        ("", 0),
    ],
)
def test_declared_size_is_read_only_from_a_labelled_field(text, expected):
    assert SehuatangExtractor.parse_size_bytes(text) == expected


def test_real_threads_declare_a_size_and_claim_no_resolution():
    extractor = SehuatangExtractor()
    for path in sorted(FIXTURES.glob("thread-*.html")):
        details = extractor.extract_details(path.read_text(encoding="utf-8"))
        assert details["size_bytes"] > 0, path
        # Board 103 posts state a capacity but never a resolution.
        assert details["resolution_hint"] is None, path


def test_site_chrome_never_supplies_a_resolution_claim():
    # Discuz! renders a "visited boards" list on every thread, and one of those
    # board names is "4K原版". Reading it would hand every thread a false 4K claim.
    html = (FIXTURES / "thread-0.html").read_text(encoding="utf-8")
    chrome = html + '<div id="visitedforums_menu"><ul><li><a>4K原版</a></li></ul></div>'
    assert SehuatangExtractor().extract_details(chrome)["resolution_hint"] is None


def test_resolution_claim_is_read_from_the_post_body():
    html = '<td class="t_f" id="postmessage_1">【影片容量】：24.5GB 2160p HDR</td>'
    details = SehuatangExtractor().extract_details(html)
    assert details["resolution_hint"] == "2160p"
    assert details["size_bytes"] == int(24.5 * 1024**3)


def test_sanitization_preserves_the_declared_size():
    # Evidence fixtures are sanitized copies; parsing must agree with the raw page,
    # otherwise selection would rank on one input and be audited against another.
    extractor = SehuatangExtractor()
    for path in sorted(FIXTURES.glob("thread-*.html")):
        raw = path.read_text(encoding="utf-8")
        assert extractor.extract_details(sanitized_fixture(raw, {})) == extractor.extract_details(raw)
