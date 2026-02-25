"""Cookie parsing helpers.

We support two input forms:
- Raw HTTP Cookie header strings: ``k=v; k2=v2`` (optionally newline-separated).
- Netscape cookie files (7 tab-separated columns), as exported by browser plugins.

This module intentionally avoids logging cookie values.
"""

from __future__ import annotations

from pathlib import Path


def _parse_tabular_cookie_line(line: str) -> tuple[str, str] | None:
    """Parse one tab-separated cookie row from Netscape or browser exports.

    Supported formats:
    - Netscape cookie file rows:
      ``domain<TAB>TRUE<TAB>path<TAB>FALSE<TAB>expiry<TAB>name<TAB>value``
    - Browser table exports (e.g. DevTools / extensions):
      ``name<TAB>value<TAB>domain<TAB>path<TAB>...``
    """
    if "\t" not in line:
        return None

    parts = [p.strip() for p in line.split("\t")]
    if len(parts) < 2:
        return None

    # Netscape cookie file format has boolean flags in columns 2 and 4.
    if len(parts) >= 7 and parts[1] in {"TRUE", "FALSE"} and parts[3] in {"TRUE", "FALSE"}:
        name = parts[5].strip()
        value = parts[6].strip()
        if name:
            return name, value
        return None

    # Browser table exports usually look like: name, value, domain, path, ...
    if len(parts) >= 4:
        name = parts[0].strip()
        value = parts[1].strip()
        domain = parts[2].strip()
        path = parts[3].strip()
        if name and domain and "." in domain and path.startswith("/"):
            return name, value

    return None


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse a raw Cookie header into a dict."""
    cookies: dict[str, str] = {}
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        tabular = _parse_tabular_cookie_line(raw_line.rstrip("\n"))
        if tabular is not None:
            key, value = tabular
            cookies[key] = value
            continue
        if "\t" in raw_line:
            # Tabular export row that we couldn't parse (e.g. missing name column).
            # Skip it rather than mis-parsing timestamps via the ':' fallback.
            continue

        for token in (x for x in line.split(";") if x.strip()):
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

        tabular = _parse_tabular_cookie_line(raw_line)
        if tabular is not None:
            name, value = tabular
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
