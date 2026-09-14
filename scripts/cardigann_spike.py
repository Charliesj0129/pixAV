#!/usr/bin/env python3
"""Collect bounded real Cardigann inputs without loading production DB/Redis settings.

Only sanitized DOM extracts are saved. Zero valid candidates is BLOCKED, never parity PASS.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from pixav.shared.cookies import load_cookies
from pixav.shared.watermark import is_known_watermark, is_watermark_info_hash
from pixav.sht_probe.flaresolverr_client import FlareSolverrSession
from pixav.sht_probe.sehuatang import SehuatangCrawler, SehuatangExtractor
from scripts.backup_files import create_backup_file


def public_thread_path(href: str) -> str | None:
    parsed = urlsplit(href)
    if parsed.netloc and parsed.hostname not in {"sehuatang.org", "www.sehuatang.org"}:
        return None
    path = parsed.path.lstrip("/")
    if re.fullmatch(r"thread-\d+-\d+-\d+\.html", path):
        return path
    query = parse_qs(parsed.query)
    tid = query.get("tid", [""])[0]
    if path == "forum.php" and query.get("mod") == ["viewthread"] and tid.isdigit():
        return f"forum.php?mod=viewthread&tid={tid}"
    return None


def board_index_path(href: str) -> str | None:
    """Public board-listing paths only, so the index can be enumerated live.

    The 4K board's id is not written down anywhere in this repository, and
    guessing it would put the run on the wrong board. It has to be read off the
    site's own index.
    """
    parsed = urlsplit(href)
    if parsed.netloc and parsed.hostname not in {"sehuatang.org", "www.sehuatang.org"}:
        return None
    path = parsed.path.lstrip("/")
    if re.fullmatch(r"forum-\d+-\d+\.html", path):
        return path
    query = parse_qs(parsed.query)
    fid = query.get("fid", [""])[0]
    if path == "forum.php" and query.get("mod") == ["forumdisplay"] and fid.isdigit():
        return f"forum-{fid}-1.html"
    return None


def attachment_reference(node) -> tuple[str, str] | None:
    """A thread's attached .torrent, as (numeric aid, raw href).

    The raw href carries a per-session download token, so it is used once inside
    the authenticated crawl and never written to evidence; only the numeric aid
    and the resulting bytes are kept.
    """
    identifier = str(node.get("id") or "")
    href = str(node.get("href") or "")
    match = re.fullmatch(r"aid(\d+)", identifier)
    if not match or not href:
        return None
    if not node.get_text(" ", strip=True).casefold().endswith(".torrent"):
        return None
    return match[1], href


def looks_like_torrent(payload: bytes) -> bool:
    """Reject an expired-login HTML page without claiming to parse bencode.

    qBittorrent remains the authoritative parser; this only refuses obvious
    non-torrent bodies before they reach it.
    """
    return (
        len(payload) >= 100 and len(payload) <= 8 * 1024 * 1024 and payload.startswith(b"d") and b"4:infod" in payload
    )


def sanitized_fixture(html: str, cookies: dict[str, str]) -> str:
    """Keep source selectors/content used by extraction; discard executable/credential DOM."""
    soup = BeautifulSoup(html, "lxml")
    for node in soup.select("script, style, input, iframe, img, meta, link"):
        node.decompose()
    for node in soup.select("form"):
        node.unwrap()
    for node in soup.select("#um, #myprompt, #toptb"):
        node.decompose()
    _sanitize_attributes(soup)
    result = str(soup)
    for value in cookies.values():
        if len(value) >= 6:
            result = result.replace(value, "REDACTED")
    # Remove plain-text URLs and email addresses outside retained magnet attributes.
    result = re.sub(r"https?://[^\s<>\"']+", "REDACTED_URL", result)
    result = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "REDACTED_EMAIL", result)
    return result


def _sanitize_attributes(soup: BeautifulSoup) -> None:
    for node in soup.find_all(True):
        attrs = {}
        for name in ("id", "class"):
            if node.get(name):
                attrs[name] = node[name]
        watermark = str(node.get("data-cfemail") or "")
        if is_known_watermark(watermark):
            attrs["data-cfemail"] = watermark
        href = str(node.get("href") or "")
        # Only public thread paths and magnets survive, never auth/download tokens.
        thread_path = public_thread_path(href)
        if thread_path:
            attrs["href"] = thread_path
        elif href.startswith("magnet:?"):
            match = re.search(r"xt=urn:btih:([a-fA-F0-9]{40})", href)
            if match:
                attrs["href"] = "magnet:?xt=urn:btih:" + match[1]
        node.attrs = attrs


def sanitized_age_gate(html: str, cookies: dict[str, str]) -> str:
    """Preserve the observed safeid assignment shape, replacing its dynamic value."""
    safe = sanitized_fixture(html, cookies)
    token = SehuatangCrawler._extract_safeid(html)
    if token and SehuatangCrawler._looks_like_age_gate(html):
        safe = safe.replace(token, "REDACTED_SAFEID")
        assignment = re.search(r"var safeid=[^;\n]+", html)
        if assignment:
            safe += "\n<script>" + assignment[0].replace(token, "REDACTED_SAFEID") + ";</script>"
    return safe


class InputCrawler(SehuatangCrawler):
    """Isolated harness observer; capture responses before any age-gate handling."""

    def __init__(self, *args, output: Path, cookies: dict[str, str], **kwargs):
        super().__init__(*args, **kwargs)
        self.output = output
        self.original_cookies = cookies
        self.raw_gates: list[dict] = []

    def capture_gate(self, html: str) -> None:
        if not self._looks_like_age_gate(html):
            return
        safe = sanitized_age_gate(html, {**self.original_cookies, **self._cookies})
        name = f"raw-age-gate-{len(self.raw_gates)}.html"
        with create_backup_file(self.output / name) as handle:
            handle.write(safe)
        self.raw_gates.append(
            {
                "file": name,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "raw_sha256": hashlib.sha256(html.encode()).hexdigest(),
                "sha256": hashlib.sha256(safe.encode()).hexdigest(),
                "recognition_preserved": self._looks_like_age_gate(safe),
            }
        )

    async def _get_client(self):
        client = await super()._get_client()
        if self._observe_response not in client.event_hooks["response"]:
            client.event_hooks["response"].append(self._observe_response)
        return client

    async def _observe_response(self, response):
        await response.aread()
        self.capture_gate(response.text)

    async def _handle_age_gate_if_needed(self, url: str, html: str) -> str:
        self.capture_gate(html)
        return await super()._handle_age_gate_if_needed(url, html)


async def collect_boards(args: argparse.Namespace) -> dict:
    """Enumerate the site's own board index so a board is chosen, never guessed."""
    index_url = urljoin(str(args.board), "/forum.php")
    if urlsplit(index_url).hostname not in {"sehuatang.org", "www.sehuatang.org"}:
        raise ValueError("spike is restricted to Sehuatang")
    cookies, _ = load_cookies(cookie_file=str(args.cookie_file), cookie_header="")
    if not cookies:
        return {"status": "BLOCKED", "reason": "cookie_missing", "boards": []}
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    crawler = InputCrawler(FlareSolverrSession(args.flaresolverr), timeout=60, output=args.output, cookies=cookies)
    crawler.seed_cookies(cookies)
    try:
        html = await crawler.fetch_page_html(index_url)
        soup = BeautifulSoup(html, "lxml")
        boards: dict[str, dict] = {}
        for node in soup.find_all("a", href=True):
            path = board_index_path(str(node["href"]))
            name = node.get_text(" ", strip=True)
            if path is None or not name:
                continue
            fid = path.split("-")[1]
            # First occurrence wins: the nav list carries the descriptive name.
            boards.setdefault(fid, {"fid": fid, "name": name, "path": path})
        safe = sanitized_fixture(html, cookies)
        with create_backup_file(args.output / "board-index.html") as handle:
            handle.write(safe)
        listing = sorted(boards.values(), key=lambda item: int(item["fid"]))
        with create_backup_file(args.output / "boards.json") as handle:
            json.dump(listing, handle, ensure_ascii=False, indent=2)
        return {
            "status": "BOARDS_READY" if listing else "BLOCKED",
            "boards": listing,
            "directory": str(args.output),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "raw_sha256": hashlib.sha256(html.encode()).hexdigest(),
            "sha256": hashlib.sha256(safe.encode()).hexdigest(),
            "age_gate": crawler._looks_like_age_gate(html),
        }
    finally:
        await crawler.aclose()


async def fetch_attachment(crawler: InputCrawler, thread_url: str, href: str) -> bytes | None:
    """Fetch one .torrent inside the authenticated session; the token never lands."""
    client = await crawler._get_client()
    try:
        response = await client.get(urljoin(thread_url, href))
        response.raise_for_status()
        payload = response.content
    except (httpx.HTTPError, ValueError):
        return None
    return payload if looks_like_torrent(payload) else None


async def capture_torrents(
    crawler: InputCrawler,
    source: str,
    html: str,
    candidates: list,
    directory: Path,
) -> dict[str, dict]:
    """Save each thread's attached .torrent, keyed by the magnet it belongs to.

    Every magnet this site publishes is bare: no ``&tr=``, so a client has only
    DHT to work with. The attached torrent carries the uploader's own trackers.

    Off by default. Measured 2026-09-11: the attachment endpoint answers this
    client with a Cloudflare interstitial (HTTP 403), because ``cf_clearance``
    is bound to a real browser's TLS fingerprint and httpx cannot present one.
    Leaving it on would spend one refused request per thread. Extra trackers
    supply the same peers and do work, so that is the default route.
    """
    references = [
        reference
        for node in BeautifulSoup(html, "lxml").find_all("a", href=True)
        if (reference := attachment_reference(node)) is not None
    ]
    if len(references) != 1 or len(candidates) != 1:
        # Ambiguous pairing; fall back to the magnet rather than guess.
        return {}
    aid, href = references[0]
    payload = await fetch_attachment(crawler, source, href)
    if payload is None:
        return {}
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(payload).hexdigest()
    name = f"{digest[:16]}.torrent"
    target = directory / name
    if not target.exists():
        with create_backup_file(target, binary=True) as handle:
            handle.write(payload)
    return {
        candidates[0].uri: {
            "torrent_file": str(Path("torrents") / name),
            "torrent_sha256": digest,
            "attachment_aid": aid,
        }
    }


async def collect(args: argparse.Namespace) -> dict:
    if urlsplit(args.board).hostname not in {"sehuatang.org", "www.sehuatang.org"}:
        raise ValueError("spike is restricted to Sehuatang")
    cookies, _ = load_cookies(cookie_file=str(args.cookie_file), cookie_header="")
    if not cookies:
        return {"status": "BLOCKED", "reason": "cookie_missing", "valid_candidates": 0}
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    crawler = InputCrawler(FlareSolverrSession(args.flaresolverr), timeout=60, output=args.output, cookies=cookies)
    crawler.seed_cookies(cookies)
    extractor = SehuatangExtractor()
    evidence = []
    baseline = []
    try:
        board = await crawler.fetch_page_html(args.board)
        soup = BeautifulSoup(board, "lxml")
        links = list(
            dict.fromkeys(
                urljoin(args.board, path)
                for a in soup.select("[id^=normalthread_] a.xst[href]")
                if (path := public_thread_path(str(a["href"]))) is not None
            )
        )[: args.max_threads]
        pages = [("board", args.board, board)]
        for index, link in enumerate(links):
            pages.append((f"thread-{index}", link, await crawler.fetch_page_html(link)))
            await asyncio.sleep(2)
        torrents = args.output / "torrents"
        for kind, source, html in pages:
            candidates = await extractor.extract_candidates(html, source) if kind != "board" else []
            details = extractor.extract_details(html) if candidates else {"size_bytes": 0, "resolution_hint": None}
            attachment = (
                await capture_torrents(crawler, source, html, candidates, torrents)
                if candidates and getattr(args, "fetch_attachments", False)
                else {}
            )
            baseline.extend(
                {
                    "title": c.title,
                    "magnet_uri": c.uri,
                    "source_url": c.source_url,
                    "size": details["size_bytes"],
                    "seeders": 0,
                    "resolution_hint": details["resolution_hint"],
                    **attachment.get(c.uri, {}),
                }
                for c in candidates
            )
            hashes = re.findall(r"\b[a-fA-F0-9]{40}\b", html)
            safe = sanitized_fixture(html, cookies)
            # Sanitization must preserve candidate recall for a usable fixture.
            after = await extractor.extract_candidates(safe, source) if kind != "board" else []
            before_hashes = {re.search(r"btih:([a-fA-F0-9]{40})", c.uri)[1].lower() for c in candidates}
            after_hashes = {re.search(r"btih:([a-fA-F0-9]{40})", c.uri)[1].lower() for c in after}
            with create_backup_file(args.output / f"{kind}.html") as handle:
                handle.write(safe)
            evidence.append(
                {
                    "kind": kind,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "raw_sha256": hashlib.sha256(html.encode()).hexdigest(),
                    "bare_hash_only": bool(before_hashes) and "magnet:?" not in html,
                    "source_path": public_thread_path(source) if kind != "board" else urlsplit(source).path,
                    "sha256": hashlib.sha256(safe.encode()).hexdigest(),
                    "valid_candidates": len(before_hashes),
                    "watermarks": sum(is_watermark_info_hash(h) for h in hashes),
                    "age_gate": crawler._looks_like_age_gate(html),
                    "sanitized_recall_equal": before_hashes == after_hashes,
                }
            )
        valid = sum(e["valid_candidates"] for e in evidence)
        with create_backup_file(args.output / "baseline.json") as handle:
            json.dump(baseline, handle, indent=2)
        return {
            "status": "INPUT_READY" if valid and all(e["sanitized_recall_equal"] for e in evidence) else "BLOCKED",
            "valid_candidates": valid,
            "parity": "NOT_RUN",
            "fixtures": evidence,
            "raw_age_gates": crawler.raw_gates,
        }
    finally:
        await crawler.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", default="https://www.sehuatang.org/forum-103-1.html")
    parser.add_argument("--cookie-file", type=Path, default=Path("secrets/sehuatang-cookies.txt"))
    parser.add_argument("--flaresolverr", default="http://127.0.0.1:18191")
    parser.add_argument("--max-threads", type=int, choices=range(1, 11), default=5)
    parser.add_argument("--fetch-attachments", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)  # Upstream HTTP errors can contain query/cookie material.
    try:
        result = asyncio.run(collect(args))
    except Exception as exc:
        result = {
            "status": "BLOCKED",
            "reason": "input_fetch_failed",
            "error_type": type(exc).__name__,
            "parity": "NOT_RUN",
        }
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with create_backup_file(args.output / "evidence.json") as handle:
        json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
