"""Tests for GooglePhotosResolver."""

from __future__ import annotations

import httpx
import pytest
import respx

from pixav.shared.exceptions import ResolveError
from pixav.strm_resolver.resolver import GooglePhotosResolver


@pytest.fixture
def resolver() -> GooglePhotosResolver:
    return GooglePhotosResolver(timeout=5)


class TestGooglePhotosResolver:
    @respx.mock
    async def test_resolve_success(self, resolver: GooglePhotosResolver) -> None:
        page_html = """
        <html>
        <body>
        <meta property="og:image" content="https://lh3.googleusercontent.com/pw/ABCDEF=w1920-h1080">
        </body>
        </html>
        """
        respx.get("https://photos.app.goo.gl/test123").mock(return_value=httpx.Response(200, text=page_html))

        cdn_url = await resolver.resolve("https://photos.app.goo.gl/test123")

        assert cdn_url == "https://lh3.googleusercontent.com/pw/ABCDEF=dv"

    @respx.mock
    async def test_resolve_no_cdn_url(self, resolver: GooglePhotosResolver) -> None:
        respx.get("https://photos.app.goo.gl/empty").mock(
            return_value=httpx.Response(200, text="<html>no cdn here</html>")
        )

        with pytest.raises(ResolveError, match="no CDN URL found"):
            await resolver.resolve("https://photos.app.goo.gl/empty")

    @respx.mock
    async def test_resolve_http_error(self, resolver: GooglePhotosResolver) -> None:
        respx.get("https://photos.app.goo.gl/bad").mock(return_value=httpx.Response(404))

        with pytest.raises(ResolveError, match="returned 404"):
            await resolver.resolve("https://photos.app.goo.gl/bad")

    @respx.mock
    async def test_resolve_connection_error(self, resolver: GooglePhotosResolver) -> None:
        respx.get("https://photos.app.goo.gl/fail").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(ResolveError, match="failed to fetch"):
            await resolver.resolve("https://photos.app.goo.gl/fail")
