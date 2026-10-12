"""Synthetic private cookies exercise scope and redirect rejection on the wire."""

from pathlib import Path

import httpx
import pytest
import respx

from pixav.config import Settings
from pixav.shared.exceptions import CrawlError
from pixav.sht_probe.crawler import HttpxCrawler
from pixav.sht_probe.flaresolverr_client import FlareSolverrSession
from pixav.sht_probe.sehuatang import SehuatangCrawler
from pixav.sht_probe.source_session import SourceSessionRequiredError, load_source_session, validate_session_response


def session(tmp_path: Path):
    path = tmp_path / "session.cookies"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_example.test\tFALSE\t/private\tTRUE\t0\thost\tsynthetic-host\n"
        ".example.test\tTRUE\t/\tFALSE\t0\tdomain\tsynthetic-domain\n"
        ".example.test\tTRUE\t/\tFALSE\t1\texpired\tsynthetic-expired\n",
        encoding="utf-8",
    )
    return load_source_session(
        cookie_header="",
        cookie_file=str(path),
        allowed_urls=["https://example.test/", "http://example.test/", "https://child.example.test/"],
    )


def test_cookie_scope_and_safe_repr(tmp_path):
    value = session(tmp_path)
    assert value.header("https://example.test/private/one") == "host=synthetic-host; domain=synthetic-domain"
    assert value.header("https://example.test/privately") == "domain=synthetic-domain"
    assert value.header("http://example.test/private") == "domain=synthetic-domain"
    assert value.header("https://child.example.test/private") == "domain=synthetic-domain"
    assert "synthetic" not in repr(value)
    assert value.records[0].has_nonstandard_attr("HttpOnly")
    with pytest.raises(CrawlError, match="allowlist"):
        value.header("https://evil.test/")


@respx.mock
async def test_redirect_is_checked_before_request_and_not_fallback(tmp_path):
    first = respx.get("https://example.test/private").mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.test/"})
    )
    outside = respx.get("https://evil.test/").mock(return_value=httpx.Response(200))
    crawler = HttpxCrawler()
    crawler.seed_cookies(session(tmp_path))
    with pytest.raises(CrawlError, match="allowlist"):
        await crawler.fetch_page_html("https://example.test/private")
    assert "host=synthetic-host" in first.calls[0].request.headers["cookie"]
    assert not outside.called


@respx.mock
async def test_server_cookie_keeps_scope_across_redirect(tmp_path):
    respx.get("https://example.test/private").mock(
        return_value=httpx.Response(
            302, headers={"Location": "/public", "Set-Cookie": "limited=synthetic; Path=/private; Secure"}
        )
    )
    second = respx.get("https://example.test/public").mock(return_value=httpx.Response(200, text="ok"))
    value = session(tmp_path)
    async with httpx.AsyncClient() as client:
        assert (await value.fetch(client, "https://example.test/private")).text == "ok"
    assert second.calls[0].request.headers["cookie"] == "domain=synthetic-domain"
    assert "limited=synthetic" in value.header("https://example.test/private")


def test_invalid_export_exception_does_not_contain_secret(tmp_path):
    path = tmp_path / "cookie"
    path.write_text("invalid\tsynthetic-sensitive-cookie")
    with pytest.raises(ValueError) as error:
        load_source_session(cookie_header="", cookie_file=str(path), allowed_urls=[])
    assert "synthetic-sensitive-cookie" not in str(error.value)


@respx.mock
async def test_duplicate_server_cookie_names_preserve_both_paths(tmp_path):
    respx.get("https://example.test/public").mock(
        return_value=httpx.Response(
            200, text="ok", headers=[("Set-Cookie", "same=one; Path=/one"), ("Set-Cookie", "same=two; Path=/two")]
        )
    )
    value = session(tmp_path)
    crawler = HttpxCrawler()
    crawler.seed_cookies(value)
    assert await crawler.fetch_page_html("https://example.test/public") == "ok"
    assert "same=one" in value.header("https://example.test/one")
    assert "same=two" in value.header("https://example.test/two")


@pytest.mark.parametrize(
    "html", ["<html>guest</html>", '<form id="loginform"></form>', '<div class="cf-chl-widget"></div>']
)
def test_guest_success_and_challenge_require_operator(html):
    response = httpx.Response(200, text=html, request=httpx.Request("GET", "https://www.sehuatang.org/"))
    with pytest.raises(SourceSessionRequiredError, match="USER_ACTION_REQUIRED"):
        validate_session_response(response)


def test_authenticated_empty_list_is_distinct_from_parser_failure():
    logout = '<a href="member.php?mod=logging&amp;action=logout">sign out</a>'
    request = httpx.Request("GET", "https://www.sehuatang.org/")
    validate_session_response(httpx.Response(200, text=logout + '<div id="threadlist"></div>', request=request))
    with pytest.raises(CrawlError, match="parser contract"):
        validate_session_response(httpx.Response(200, text=logout, request=request))


def test_passive_cloudflare_detection_is_not_a_login_challenge():
    html = (
        '<a href="member.php?mod=logging&amp;action=logout">sign out</a>'
        '<div id="threadlist"></div>'
        '<script>load("/cdn-cgi/challenge-platform/h/b/scripts/jsd/main.js")</script>'
    )
    validate_session_response(
        httpx.Response(200, text=html, request=httpx.Request("GET", "https://www.sehuatang.org/"))
    )


@pytest.mark.parametrize(
    "headers,body",
    [({"cf-mitigated": "challenge"}, ""), ({}, "window._cf_chl_opt={}"), ({}, '<form id="challenge-form"></form>')],
)
def test_interstitial_challenge_remains_manual_even_with_authenticated_markup(headers, body):
    html = '<a href="member.php?mod=logging&amp;action=logout">sign out</a><div id="threadlist"></div>' + body
    with pytest.raises(SourceSessionRequiredError, match="USER_ACTION_REQUIRED"):
        validate_session_response(
            httpx.Response(200, headers=headers, text=html, request=httpx.Request("GET", "https://www.sehuatang.org/"))
        )


@pytest.mark.parametrize("retained", [False, True])
@respx.mock
async def test_source_user_agent_reaches_request_without_changing_cookie_scope(tmp_path, retained):
    route = respx.get("https://example.test/private").mock(return_value=httpx.Response(200, text="ok"))
    agent = "Mozilla/5.0 Synthetic Browser"
    if retained:
        crawler = SehuatangCrawler(FlareSolverrSession("http://unused.test"), user_agent=agent, request_delay_seconds=0)
    else:
        crawler = HttpxCrawler(user_agent=agent)
    crawler.seed_cookies(session(tmp_path))
    try:
        assert await crawler.fetch_page_html("https://example.test/private") == "ok"
        assert route.calls[0].request.headers["user-agent"] == agent
        assert "host=synthetic-host" in route.calls[0].request.headers["cookie"]
    finally:
        if retained:
            await crawler.aclose()


def test_source_user_agent_rejects_header_injection():
    with pytest.raises(ValueError):
        Settings(crawl_user_agent="Synthetic\r\nCookie: injected")


def test_raw_source_cookie_is_absent_from_settings_repr():
    assert "synthetic-private-cookie" not in repr(Settings(crawl_cookie_header="session=synthetic-private-cookie"))
