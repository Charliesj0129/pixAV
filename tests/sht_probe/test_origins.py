"""Reject URL components that merely mention a trusted adapter hostname."""

import pytest

from pixav.sht_probe.origins import is_retained_source_url


@pytest.mark.parametrize(
    "url",
    [
        "https://www.sehuatang.org/forum-1.html",
        "https://SEHUATANG.ORG:443/",
        "http://sehuatang.org:80/",
    ],
)
def test_accepts_explicit_origins(url: str) -> None:
    assert is_retained_source_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/sehuatang.org",
        "https://example.test/?origin=sehuatang.org",
        "https://sehuatang.org.example.test/",
        "https://sehuatang.org@example.test/",
        "https://user@sehuatang.org/",
        "https://www.sehuatang.org:8443/",
        "ftp://sehuatang.org/",
        "//sehuatang.org/",
        "https://sehuatang.org:bad/",
        "https://[broken/",
    ],
)
def test_rejects_untrusted_origins(url: str) -> None:
    assert not is_retained_source_url(url)
