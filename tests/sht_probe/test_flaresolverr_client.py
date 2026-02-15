"""Tests for FlareSolverrSession."""

from __future__ import annotations

import httpx
import pytest
import respx

from pixav.shared.exceptions import CrawlError
from pixav.sht_probe.flaresolverr_client import FlareSolverrSession


@pytest.fixture
def session() -> FlareSolverrSession:
    return FlareSolverrSession(base_url="http://flaresolverr:8191")


class TestFlareSolverrSession:
    @respx.mock
    async def test_get_html_success(self, session: FlareSolverrSession) -> None:
        mock_response = {
            "status": "ok",
            "message": "",
            "solution": {
                "url": "https://target.com/page",
                "status": 200,
                "response": "<html><body>Solved!</body></html>",
                "cookies": [{"name": "cf_clearance", "value": "abc"}],
            },
        }
        respx.post("http://flaresolverr:8191/v1").mock(return_value=httpx.Response(200, json=mock_response))

        html, cookies = await session.get_html("https://target.com/page", timeout=30)
        assert "<html>" in html
        assert "Solved!" in html
        assert cookies == {"cf_clearance": "abc"}

    @respx.mock
    async def test_get_html_challenge_failed(self, session: FlareSolverrSession) -> None:
        mock_response = {
            "status": "error",
            "message": "Challenge not solved",
            "solution": {},
        }
        respx.post("http://flaresolverr:8191/v1").mock(return_value=httpx.Response(200, json=mock_response))

        with pytest.raises(CrawlError, match="FlareSolverr error"):
            await session.get_html("https://target.com/page")

    @respx.mock
    async def test_get_html_empty_response(self, session: FlareSolverrSession) -> None:
        mock_response = {
            "status": "ok",
            "message": "",
            "solution": {"response": ""},
        }
        respx.post("http://flaresolverr:8191/v1").mock(return_value=httpx.Response(200, json=mock_response))

        with pytest.raises(CrawlError, match="empty response"):
            await session.get_html("https://target.com/page")

    @respx.mock
    async def test_get_html_http_error(self, session: FlareSolverrSession) -> None:
        respx.post("http://flaresolverr:8191/v1").mock(return_value=httpx.Response(500, text="Server Error"))

        with pytest.raises(CrawlError, match="FlareSolverr returned 500"):
            await session.get_html("https://target.com/page")

    @respx.mock
    async def test_get_html_connection_error(self, session: FlareSolverrSession) -> None:
        respx.post("http://flaresolverr:8191/v1").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(CrawlError, match="FlareSolverr request failed"):
            await session.get_html("https://target.com/page")

    @respx.mock
    async def test_get_html_sends_session_and_cookies(self, session: FlareSolverrSession) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured
            captured = request.read().decode("utf-8")
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "solution": {
                        "status": 200,
                        "response": "<html>ok</html>",
                        "cookies": [],
                    },
                },
            )

        respx.post("http://flaresolverr:8191/v1").mock(side_effect=handler)

        await session.get_html(
            "https://target.com/page",
            cookies={"foo": "bar"},
            headers={"Referer": "https://target.com/"},
        )

        assert isinstance(captured, str)
        assert '"session"' in captured
        assert '"cookies"' in captured
        assert '"foo"' in captured
        assert '".target.com"' in captured
        assert '"headers"' in captured
