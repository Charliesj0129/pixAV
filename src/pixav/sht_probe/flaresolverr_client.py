"""FlareSolverr client for Cloudflare-protected page fetching."""

from __future__ import annotations

import logging

import httpx

from pixav.shared.exceptions import CrawlError

logger = logging.getLogger(__name__)


class FlareSolverrSession:
    """Fetch HTML from Cloudflare-protected pages via FlareSolverr.

    Implements the ``FlareSolverSession`` protocol.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def get_html(self, url: str, *, timeout: int = 60) -> str:
        """Send a request through FlareSolverr and return the solved HTML.

        Args:
            url: Target page URL.
            timeout: Max time (seconds) for FlareSolverr to solve the challenge.

        Returns:
            Decoded HTML string of the target page.

        Raises:
            CrawlError: If FlareSolverr fails or returns an error.
        """
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": timeout * 1000,
        }

        try:
            async with httpx.AsyncClient(timeout=timeout + 10) as client:
                resp = await client.post(f"{self._base_url}/v1", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise CrawlError(f"FlareSolverr returned {exc.response.status_code}: {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise CrawlError(f"FlareSolverr request failed: {exc}") from exc

        status = data.get("status", "")
        if status != "ok":
            message = data.get("message", "unknown error")
            raise CrawlError(f"FlareSolverr error ({status}): {message}")

        solution = data.get("solution", {})
        html: str = solution.get("response", "")
        if not html:
            raise CrawlError("FlareSolverr returned empty response body")

        logger.info("FlareSolverr solved %s (status=%s)", url, solution.get("status", "?"))
        return html
