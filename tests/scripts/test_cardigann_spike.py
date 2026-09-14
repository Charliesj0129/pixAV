from scripts.cardigann_spike import public_thread_path, sanitized_fixture


def test_sanitizer_removes_credentials_and_executable_dom():
    html = """<html><script>secret</script><input value="secret"><a href="https://host/?token=secret">link</a>
    <a href="thread-42-1-1.html" onclick="secret">thread</a><span id="thread_subject">title</span>
    <a href="magnet:?xt=urn:btih:08ada5a7a6183aae1e09d831df6748d566095a10&dn=secret">magnet</a>
    <p>secret user@example.invalid</p></html>"""
    result = sanitized_fixture(html, {"cookie": "secret"})
    assert "secret" not in result
    assert "script" not in result and "onclick" not in result and "input" not in result
    assert "thread-42-1-1.html" in result
    assert "thread_subject" in result
    assert "user@example.invalid" not in result


def test_discuz_form_and_query_thread_survive_without_auth_tokens():
    html = '<form><tbody id="normalthread_42"><a class="s xst" href="forum.php?mod=viewthread&amp;tid=42&amp;auth=private">title</a></tbody></form>'
    result = sanitized_fixture(html, {"display_flag": "1"})
    assert "normalthread_42" in result
    assert "mod=viewthread" in result and "tid=42" in result
    assert "auth=" not in result
    assert "<form" not in result
    assert public_thread_path("https://evil.invalid/thread-42-1-1.html") is None


async def test_real_raw_age_gate_recognition_and_no_candidates():
    import hashlib
    import json
    from pathlib import Path

    from pixav.sht_probe.sehuatang import SehuatangCrawler, SehuatangExtractor
    from scripts.cardigann_spike import sanitized_age_gate

    root = Path("tests/fixtures/cardigann_20260907")
    html = (root / "age-gate.html").read_text()
    evidence = json.loads((root / "age-gate-evidence.json").read_text())["raw_age_gates"][0]
    assert hashlib.sha256(html.encode()).hexdigest() == evidence["sha256"]
    assert SehuatangCrawler._looks_like_age_gate(html)
    assert SehuatangCrawler._extract_safeid(html) == "REDACTED_SAFEID"
    assert await SehuatangExtractor().extract_candidates(html, "https://www.sehuatang.org/forum-103-1.html") == []
    assert SehuatangCrawler._looks_like_age_gate(sanitized_age_gate(html, {}))


class TestBoardIndex:
    """The 4K board's id is not written down anywhere, so it must be read live."""

    def test_accepts_only_public_board_listing_paths(self):
        from scripts.cardigann_spike import board_index_path

        assert board_index_path("forum-103-1.html") == "forum-103-1.html"
        assert board_index_path("/forum-171-2.html") == "forum-171-2.html"
        assert board_index_path("https://www.sehuatang.org/forum-5-1.html") == "forum-5-1.html"
        assert board_index_path("forum.php?mod=forumdisplay&fid=171") == "forum-171-1.html"

    def test_rejects_threads_other_hosts_and_auth_bearing_paths(self):
        from scripts.cardigann_spike import board_index_path

        assert board_index_path("thread-42-1-1.html") is None
        assert board_index_path("forum.php?mod=viewthread&tid=42") is None
        assert board_index_path("https://evil.invalid/forum-1-1.html") is None
        assert board_index_path("forum.php?mod=forumdisplay&fid=171&auth=private") == "forum-171-1.html"
        assert board_index_path("") is None


class TestTorrentAttachment:
    def test_only_torrent_anchors_carrying_a_numeric_aid_are_referenced(self):
        from bs4 import BeautifulSoup

        from scripts.cardigann_spike import attachment_reference

        soup = BeautifulSoup(
            '<a id="aid21739811" href="forum.php?mod=attachment&amp;aid=TOKEN">R-1.torrent</a>'
            '<a id="aid21739812" href="x">R-1.jpg</a>'
            '<a id="nope" href="y">R-2.torrent</a>'
            '<a id="aid3">R-3.torrent</a>',
            "lxml",
        )
        found = [attachment_reference(node) for node in soup.find_all("a")]
        assert found == [("21739811", "forum.php?mod=attachment&aid=TOKEN"), None, None, None]

    def test_an_expired_login_page_is_never_mistaken_for_a_torrent(self):
        from scripts.cardigann_spike import looks_like_torrent

        assert looks_like_torrent(b"d8:announce20:http://t/announce.x4:infod4:name3:abcee" + b"e" * 60)
        assert not looks_like_torrent(b"<!DOCTYPE html><html>please log in</html>")
        assert not looks_like_torrent(b"d4:infod4:name3:abcee")  # too short to be a real torrent
        assert not looks_like_torrent(b"")

    def test_attachment_token_never_reaches_a_saved_fixture(self):
        from scripts.cardigann_spike import sanitized_fixture

        html = '<a id="aid21739811" href="forum.php?mod=attachment&amp;aid=SECRETTOKEN">R-1.torrent</a>'
        result = sanitized_fixture(html, {})
        assert "SECRETTOKEN" not in result
        assert "mod=attachment" not in result
        # The anchor id survives so the attachment remains attributable.
        assert "aid21739811" in result
