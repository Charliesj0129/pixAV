"""Scoped private source sessions and bounded redirects at the HTTP boundary."""

from __future__ import annotations

import re
import time
from http.cookiejar import Cookie
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from pixav.shared.cookies import parse_cookie_header
from pixav.shared.exceptions import CrawlError
from pixav.sht_probe.origins import is_retained_source_url


class SourceSessionRequiredError(CrawlError):
    """Stop discovery until an operator replaces or verifies the session."""


def validate_session_response(response: httpx.Response) -> None:
    """HTTP success alone cannot establish an authenticated source page."""
    if response.status_code in {401, 403}:
        raise SourceSessionRequiredError("USER_ACTION_REQUIRED: source session rejected")
    html = response.text
    # Cloudflare injects passive JavaScript Detections into ordinary HTML too.
    # The shared challenge-platform path alone is not an interstitial challenge.
    if response.headers.get("cf-mitigated", "").lower() == "challenge" or any(
        marker in html.lower() for marker in ("cf-chl-", "_cf_chl_opt", "challenge-form", "g-recaptcha", "hcaptcha")
    ):
        raise SourceSessionRequiredError("USER_ACTION_REQUIRED: source login challenge")
    if not is_retained_source_url(str(response.url)):
        return
    soup = BeautifulSoup(html, "lxml")
    authenticated = any(
        "mod=logging" in str(tag.get("href", "")) and "action=logout" in str(tag.get("href", ""))
        for tag in soup.find_all("a")
    )
    if not authenticated:
        raise SourceSessionRequiredError("USER_ACTION_REQUIRED: authenticated source page not established")
    if soup.find(id="threadlist") is None and soup.find(id="postlist") is None:
        raise CrawlError("source parser contract missing; empty discovery not established")


def origin(url: str) -> tuple[str, str, int]:
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
            raise ValueError
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        return parts.scheme, parts.hostname, port
    except ValueError:
        raise CrawlError("invalid source origin") from None


class SourceSession(dict[str, str]):
    """Compatibility mapping; requests always use the scoped records instead."""

    def __init__(self, records: list[Cookie], allowed_urls: list[str]) -> None:
        super().__init__((cookie.name, cookie.value or "") for cookie in records)
        self.records = records
        self.allowed_origins = frozenset(origin(url) for url in allowed_urls)

    def __repr__(self) -> str:
        return f"SourceSession(cookie_count={len(self.records)})"

    def require_allowed(self, url: str) -> None:
        if origin(url) not in self.allowed_origins:
            raise CrawlError("source destination outside allowlist")

    def header(self, url: str) -> str:
        self.require_allowed(url)
        scheme, host, _ = origin(url)
        path = urlsplit(url).path or "/"
        matches = []
        for cookie in self.records:
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", cookie.name) or any(
                ord(char) < 0x21 or ord(char) > 0x7E or char in ";," for char in (cookie.value or "")
            ):
                raise CrawlError("invalid source cookie characters")
            domain = cookie.domain.lstrip(".").lower()
            domain_match = host == domain or (cookie.domain_specified and host.endswith("." + domain))
            cookie_path = cookie.path or "/"
            path_match = path == cookie_path or (
                path.startswith(cookie_path) and (cookie_path.endswith("/") or path[len(cookie_path) :].startswith("/"))
            )
            if (
                domain_match
                and path_match
                and (not cookie.secure or scheme == "https")
                and (cookie.expires is None or cookie.expires > time.time())
            ):
                matches.append(cookie)
        matches.sort(key=lambda cookie: -len(cookie.path or "/"))
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in matches)

    async def fetch(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        for _ in range(11):
            self.require_allowed(url)
            response = await client.get(url, headers={"Cookie": self.header(url)}, follow_redirects=False)
            # Keep server cookie scope, including duplicate names at different paths.
            for cookie in response.cookies.jar:
                self.records = [
                    old
                    for old in self.records
                    if (old.domain, old.path, old.name) != (cookie.domain, cookie.path, cookie.name)
                ]
                self.records.append(cookie)
            if not response.is_redirect:
                validate_session_response(response)
                return response
            location = response.headers.get("location")
            if not location:
                raise CrawlError("source redirect missing destination")
            url = urljoin(url, location)
        raise CrawlError("source redirect limit exceeded")


def load_source_session(  # noqa: C901 -- two input formats validated at this adapter boundary
    *, cookie_header: str, cookie_file: str, allowed_urls: list[str]
) -> SourceSession:
    """Netscape exports retain scope; raw headers bind to exact seed hosts."""
    if cookie_header.strip():
        if "\t" in cookie_header:
            raise ValueError("scoped source session requires Netscape export or raw header")
        values = parse_cookie_header(cookie_header)
        records = []
        for scheme, host, _ in {origin(url) for url in allowed_urls}:
            for name, value in values.items():
                records.append(
                    Cookie(
                        0,
                        name,
                        value,
                        None,
                        False,
                        host,
                        False,
                        False,
                        "/",
                        True,
                        scheme == "https",
                        None,
                        True,
                        None,
                        None,
                        {},
                    )
                )
    elif cookie_file.strip():
        path = Path(cookie_file)
        try:
            records = []
            for line in path.read_text(encoding="utf-8").splitlines():
                http_only = line.startswith("#HttpOnly_")
                if http_only:
                    line = line.removeprefix("#HttpOnly_")
                if not line.strip() or line.startswith("#"):
                    continue
                domain, subdomains, scope, secure, expiry, name, value = line.split("\t")
                if (
                    not domain
                    or not name
                    or not scope.startswith("/")
                    or subdomains not in {"TRUE", "FALSE"}
                    or secure not in {"TRUE", "FALSE"}
                    or any(char in name + value for char in "\r\n;")
                ):
                    raise ValueError
                expires = int(expiry)
                if expires < 0:
                    raise ValueError
                records.append(
                    Cookie(
                        0,
                        name,
                        value,
                        None,
                        False,
                        domain.lower(),
                        subdomains == "TRUE",
                        domain.startswith("."),
                        scope,
                        True,
                        secure == "TRUE",
                        expires or None,
                        expires == 0,
                        None,
                        None,
                        {"HttpOnly": ""} if http_only else {},
                    )
                )
        except FileNotFoundError:
            raise FileNotFoundError("source cookie file missing") from None
        except Exception:
            raise ValueError("invalid scoped source cookie export") from None
    else:
        records = []
    if (cookie_header.strip() or cookie_file.strip()) and not records:
        raise ValueError("configured crawler cookies are empty or invalid")
    return SourceSession(records, allowed_urls)
