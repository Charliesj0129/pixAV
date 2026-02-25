"""Tests for shared/cookies.py — cookie parsing helpers."""

from __future__ import annotations

import pathlib

import pytest

from pixav.shared.cookies import load_cookies, parse_cookie_file, parse_cookie_header


class TestParseCookieHeader:
    def test_simple_kv(self) -> None:
        result = parse_cookie_header("session=abc123; user=bob")
        assert result == {"session": "abc123", "user": "bob"}

    def test_single_cookie(self) -> None:
        result = parse_cookie_header("token=xyz")
        assert result == {"token": "xyz"}

    def test_strips_whitespace(self) -> None:
        result = parse_cookie_header("  a = 1 ;  b = 2 ")
        assert result == {"a": "1", "b": "2"}

    def test_multiline_input(self) -> None:
        result = parse_cookie_header("a=1\nb=2")
        assert result == {"a": "1", "b": "2"}

    def test_empty_string_returns_empty(self) -> None:
        assert parse_cookie_header("") == {}

    def test_colon_separator(self) -> None:
        result = parse_cookie_header("key:value")
        assert result == {"key": "value"}

    def test_value_may_contain_equals(self) -> None:
        # Only first '=' splits key/value
        result = parse_cookie_header("token=abc=def")
        assert result == {"token": "abc=def"}

    def test_skip_tokens_without_separator(self) -> None:
        # Tokens with neither '=' nor ':' are skipped
        result = parse_cookie_header("noseparator; valid=yes")
        assert result == {"valid": "yes"}

    def test_empty_key_skipped(self) -> None:
        result = parse_cookie_header("=value; good=ok")
        assert result == {"good": "ok"}

    def test_browser_tabular_rows(self) -> None:
        raw = "\n".join(
            [
                "cf_clearance\tabc123\t.sehuatang.org\t/\t2027-02-24T16:10:21.473Z\t310\t✓\t✓\tNone\t\t\tMedium",
                "cPNj_2132_auth\ttoken456\twww.sehuatang.org\t/\t2026-03-16T16:11:20.847Z\t113\t✓\t✓\t\t\t\tMedium",
            ]
        )
        result = parse_cookie_header(raw)
        assert result == {
            "cf_clearance": "abc123",
            "cPNj_2132_auth": "token456",
        }

    def test_browser_tabular_row_missing_name_is_skipped(self) -> None:
        raw = "\tonA4F2xz9o7QF2f9\twww.sehuatang.org\t/\t2027-02-14T16:11:01.000Z\t21\t\t\t\t\t\t\tMedium"
        assert parse_cookie_header(raw) == {}


class TestParseCookieFile:
    def test_netscape_format(self, tmp_path: pathlib.Path) -> None:
        content = "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".example.com\tTRUE\t/\tFALSE\t0\tsession_id\tabc123",
                ".example.com\tTRUE\t/\tFALSE\t0\tuser_id\t42",
            ]
        )
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text(content, encoding="utf-8")
        result = parse_cookie_file(pathlib.Path(cookie_file))
        assert result == {"session_id": "abc123", "user_id": "42"}

    def test_skips_comments_and_blank_lines(self, tmp_path: pathlib.Path) -> None:
        content = "\n".join(
            [
                "# comment",
                "",
                ".x.com\tTRUE\t/\tFALSE\t0\tname\tval",
            ]
        )
        cookie_file = tmp_path / "c.txt"
        cookie_file.write_text(content, encoding="utf-8")
        result = parse_cookie_file(pathlib.Path(cookie_file))
        assert result == {"name": "val"}

    def test_fallback_to_header_format(self, tmp_path: pathlib.Path) -> None:
        content = "a=1; b=2"
        cookie_file = tmp_path / "c.txt"
        cookie_file.write_text(content, encoding="utf-8")
        result = parse_cookie_file(pathlib.Path(cookie_file))
        assert result == {"a": "1", "b": "2"}

    def test_browser_tabular_export_format(self, tmp_path: pathlib.Path) -> None:
        content = "\n".join(
            [
                "cf_clearance\tabc123\t.sehuatang.org\t/\t2027-02-24T16:10:21.473Z\t310\t✓\t✓\tNone\t\t\tMedium",
                "cPNj_2132_saltkey\toNeNLLDo\twww.sehuatang.org\t/\t2026-03-16T14:58:10.351Z\t25\t✓\t✓\t\t\t\tMedium",
            ]
        )
        cookie_file = tmp_path / "cookies.tsv"
        cookie_file.write_text(content, encoding="utf-8")
        result = parse_cookie_file(pathlib.Path(cookie_file))
        assert result == {
            "cf_clearance": "abc123",
            "cPNj_2132_saltkey": "oNeNLLDo",
        }


class TestLoadCookies:
    def test_header_takes_priority(self) -> None:
        cookies, source = load_cookies(cookie_header="a=1", cookie_file="ignored")
        assert cookies == {"a": "1"}
        assert source == "header"

    def test_empty_inputs_return_empty(self) -> None:
        cookies, source = load_cookies()
        assert cookies == {}
        assert source == ""

    def test_file_path_not_found_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_cookies(cookie_file="/nonexistent/path/cookies.txt")

    def test_file_path_empty_file_raises(self, tmp_path: pathlib.Path) -> None:
        empty = tmp_path / "empty.txt"
        empty.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="empty or invalid"):
            load_cookies(cookie_file=str(empty))

    def test_file_returns_source_string(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "c.txt"
        f.write_text("k=v", encoding="utf-8")
        cookies, source = load_cookies(cookie_file=str(f))
        assert cookies == {"k": "v"}
        assert source.startswith("file:")
