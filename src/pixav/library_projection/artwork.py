"""Retained artwork originals (BDD-098, 099, 100).

An original is stored once under its SHA-256 and never rewritten; PostgreSQL
holds its provenance and digest. A projection reads the retained original and
checks it against that digest; it never fetches artwork from a provider, so a
provider outage cannot take a poster away from a rebuilt library.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

MAX_ARTWORK_BYTES = 20 * 1024 * 1024
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class ArtworkIntegrityError(Exception):
    """A retained original is missing, replaced, or not the recorded bytes."""


def sniff_media_type(content: bytes) -> str | None:
    """Recognise an image by its signature, never by its name."""
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _read_regular(path: Path, limit: int) -> bytes:
    """Read a regular file without following a link, refusing anything over the limit."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("artwork must be a regular file")
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"artwork is larger than {limit} bytes")
    return content


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ArtworkStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError("artwork digest must be a lowercase SHA-256")
        return self.root / sha256[:2] / sha256

    def import_file(self, source: Path) -> tuple[str, int, str]:
        """Retain one image; return its digest, size and media type."""
        content = _read_regular(source, MAX_ARTWORK_BYTES)
        media_type = sniff_media_type(content)
        if media_type is None or not content:
            raise ValueError("artwork must be a JPEG, PNG or WebP image")
        digest = hashlib.sha256(content).hexdigest()
        final = self.path_for(digest)
        final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if final.exists():
            # Content-addressed: the same digest is the same bytes, provided the
            # file there really is them.
            self.read_verified(digest, len(content))
            return digest, len(content), media_type
        descriptor, temporary = tempfile.mkstemp(dir=final.parent, prefix=".import-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, final)
        except FileExistsError:
            self.read_verified(digest, len(content))
        finally:
            os.unlink(temporary)
        _fsync_directory(final.parent)
        return digest, len(content), media_type

    def read_verified(self, sha256: str, size_bytes: int) -> bytes:
        """The retained bytes, only if they are exactly the recorded original."""
        try:
            content = _read_regular(self.path_for(sha256), MAX_ARTWORK_BYTES)
        except (OSError, ValueError) as exc:
            raise ArtworkIntegrityError(type(exc).__name__) from exc
        if len(content) != size_bytes or hashlib.sha256(content).hexdigest() != sha256:
            raise ArtworkIntegrityError("retained artwork does not match its recorded digest")
        return content
