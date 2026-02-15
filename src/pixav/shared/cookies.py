"""Cookie parsing helpers.

We support two input forms:
- Raw HTTP Cookie header strings: ``k=v; k2=v2`` (optionally newline-separated).
- Netscape cookie files (7 tab-separated columns), as exported by browser plugins.

This module intentionally avoids logging cookie values.
"""

from __future__ import annotations

from pathlib import Path


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse a raw Cookie header into a dict."""
    cookies: dict[str, str] = {}
    tokens: list[str] = []

    for part in raw.splitlines():
        tokens.extend(x for x in part.split(";") if x.strip())

    for token in tokens:
        pair = token.strip()
        if not pair:
            continue

        if "=" in pair:
            key, value = pair.split("=", 1)
        elif ":" in pair:
            # Support copy/paste in "k:v" style.
            key, value = pair.split(":", 1)
        else:
            continue

        key = key.strip()
        if key:
            cookies[key] = value.strip()

    return cookies


def parse_cookie_file(path: Path) -> dict[str, str]:
    """Parse a Netscape cookie file into a dict."""
    cookies: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")

    # Netscape cookie format: domain\tflag\tpath\tsecure\texpiry\tname\tvalue
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) >= 7:
            name = parts[5].strip()
            value = parts[6].strip()
            if name:
                cookies[name] = value
            continue

        # Fallback: support one-line `k=v; k2=v2` style file.
        cookies.update(parse_cookie_header(line))

    return cookies


def load_cookies(*, cookie_header: str = "", cookie_file: str = "") -> tuple[dict[str, str], str]:
    """Load cookies from either a raw header string or a file path.

    Returns:
        (cookies, source) where source is a short string describing where cookies came from.
    """
    header = cookie_header.strip()
    if header:
        return parse_cookie_header(header), "header"

    file_value = cookie_file.strip()
    if not file_value:
        return {}, ""

    path = Path(file_value)
    if not path.exists():
        raise FileNotFoundError(f"cookie file not found: {path}")

    parsed = parse_cookie_file(path)
    if not parsed:
        raise ValueError(f"cookie file is empty or invalid: {path}")
    return parsed, f"file:{path}"
