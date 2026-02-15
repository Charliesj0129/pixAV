"""FlareSolverr client for Cloudflare-protected page fetching."""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlparse

import httpx

from pixav.shared.exceptions import CrawlError

logger = logging.getLogger(__name__)


class FlareSolverrSession:
    """Fetch HTML from Cloudflare-protected pages via FlareSolverr.

    Implements the ``FlareSolverSession`` protocol.
    """

    def __init__(self, base_url: str, *, session_id: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._session_id = session_id or f"pixav-{uuid.uuid4().hex[:12]}"

    async def get_html(
        self,
        url: str,
        *,
        timeout: int = 60,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Send a request through FlareSolverr and return the solved HTML and cookies.

        Args:
            url: Target page URL.
            timeout: Max time (seconds) for FlareSolverr to solve the challenge.
            cookies: Optional cookies to seed the browser session.
            headers: Optional request headers (e.g. Referer).

        Returns:
            Tuple of (html_string, cookies_dict).

        Raises:
            CrawlError: If FlareSolverr fails or returns an error.
        """
        request_cookies = self._to_flaresolverr_cookies(url, cookies or {})

        payload: dict[str, object] = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": timeout * 1000,
            "session": self._session_id,
        }
        if request_cookies:
            payload["cookies"] = request_cookies
        if headers:
            payload["headers"] = headers

        try:
            async with httpx.AsyncClient(timeout=timeout + 10) as client:
                resp = await client.post(f"{self._base_url}/v1", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise CrawlError(f"FlareSolverr returned {exc.response.status_code}: {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise CrawlError(f"FlareSolverr request failed: {exc!r}") from exc

        status = data.get("status", "")
        if status != "ok":
            message = data.get("message", "unknown error")
            raise CrawlError(f"FlareSolverr error ({status}): {message}")

        solution = data.get("solution", {})
        html: str = solution.get("response", "")
        if not html:
            raise CrawlError("FlareSolverr returned empty response body")

        cookies = {c["name"]: c["value"] for c in solution.get("cookies", [])}
        logger.info("FlareSolverr solved %s (status=%s, cookies=%d)", url, solution.get("status", "?"), len(cookies))
        return html, cookies

    @staticmethod
    def _to_flaresolverr_cookies(url: str, cookies: dict[str, str]) -> list[dict[str, str]]:
        """Convert cookie dict to FlareSolverr cookie objects."""
        if not cookies:
            return []

        host = urlparse(url).hostname or ""
        if host and not host.startswith("."):
            domain = f".{host}"
        else:
            domain = host

        return [
            {
                "name": key,
                "value": value,
                "domain": domain,
                "path": "/",
            }
            for key, value in cookies.items()
            if key
        ]
