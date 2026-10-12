"""Origin checks for selecting the retained source adapter."""

from urllib.parse import urlsplit


def is_retained_source_url(url: str) -> bool:
    """Accept only the adapter's explicit HTTP origins, without credentials."""
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in {"sehuatang.org", "www.sehuatang.org"}
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443 if parsed.scheme == "https" else 80}
        )
    except ValueError:
        return False
