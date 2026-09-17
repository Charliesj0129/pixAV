"""Sanitized real board/thread inputs, captured independently of these assertions."""

import hashlib
import json
from pathlib import Path

from bs4 import BeautifulSoup

from pixav.shared.watermark import is_known_watermark
from pixav.sht_probe.sehuatang import SehuatangExtractor

ROOT = Path("tests/fixtures/sehuatang_20260907")


async def test_real_latest_thread_candidates_survive_normalization():
    provenance = json.loads((ROOT / "provenance.json").read_text())
    assert provenance["fixtures"]
    for fixture in provenance["fixtures"]:
        raw = (ROOT / fixture["file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == fixture["sha256"]
        expected = fixture["candidates"]
        assert expected, "zero valid candidates is never a passing recall fixture"
        found = await SehuatangExtractor().extract_candidates(raw.decode(), expected[0]["source_url"])
        assert {(item.uri, item.title, item.source_url) for item in found} == {
            (item["magnet_uri"], item["title"], item["source_url"]) for item in expected
        }


def test_real_latest_board_selector_and_sanitized_dom():
    board = BeautifulSoup((ROOT / "board.html").read_text(), "lxml")
    assert len(board.select('[id^="normalthread_"] a.xst[href]')) == 3
    for path in ROOT.glob("*.html"):
        soup = BeautifulSoup(path.read_text(), "lxml")
        assert not soup.select("script, input, iframe, form")
        assert not soup.select("[onclick], [onload]")


async def test_real_contact_watermark_is_not_a_candidate():
    provenance = json.loads((ROOT / "provenance.json").read_text())
    fixture = provenance["watermark_fixture"]
    raw = (ROOT / fixture["file"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == fixture["sha256"]
    soup = BeautifulSoup(raw.decode(), "lxml")
    assert is_known_watermark(soup.select_one("[data-cfemail]")["data-cfemail"])
    assert await SehuatangExtractor().extract(raw.decode()) == []
