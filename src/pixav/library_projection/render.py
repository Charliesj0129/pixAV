"""Render one video's projection: .strm, .nfo and retained artwork (BDD-087-094, 098, 099).

Every name is derived from ``video_id`` alone, so a title change updates the
item instead of creating a second one. The .strm holds only the stable pixAV
stream URL; no provider URL, share location, account or cookie is an input
here, and a final check refuses output that mentions a provider host anyway.
The digest covers each file's name, size and SHA-256, so it can be computed
from the recorded artwork digests without reading any artwork.
"""

from __future__ import annotations

import hashlib
import re
import uuid
import xml.etree.ElementTree as ET  # noqa: S405 - builds XML, never parses untrusted input
from collections.abc import Sequence
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from pixav.library_projection.artwork import EXTENSIONS
from pixav.library_projection.metadata import EffectiveMetadata
from pixav.library_projection.repository import ArtworkRecord

LAYOUT_VERSION = "jellyfin-movie-v1"
_PROVIDER_MARKERS = ("googleusercontent", "photos.app.goo.gl", "photos.google.com", "ggpht.com")
# Characters XML 1.0 cannot carry at all; a scraper can still produce them.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")


class ProjectionRefusedError(ValueError):
    """The projection would expose something it must not; nothing is written."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProjectionFile(BaseModel):
    """One file of a projection. Artwork carries no bytes: the writer reads the retained original."""

    model_config = {"frozen": True}

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)
    content: bytes | None = None


class ProjectionSpec(BaseModel):
    model_config = {"frozen": True}

    video_id: uuid.UUID
    layout_version: str = LAYOUT_VERSION
    files: tuple[ProjectionFile, ...]
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def item_name(video_id: uuid.UUID) -> str:
    return f"pixav-{video_id}"


def projection_digest(files: Sequence[ProjectionFile], layout_version: str = LAYOUT_VERSION) -> str:
    listing = "".join(f"{f.name}\0{f.size}\0{f.sha256}\n" for f in sorted(files, key=lambda f: f.name))
    return hashlib.sha256(f"{layout_version}\n{listing}".encode()).hexdigest()


def stream_url(resolver_base_url: str, video_id: uuid.UUID) -> str:
    """The stable URL players use; a base that could carry a secret or a provider host is refused."""
    parts = urlsplit(resolver_base_url.strip())
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ProjectionRefusedError("RESOLVER_BASE_URL_INVALID")
    if any(marker in parts.hostname.lower() for marker in _PROVIDER_MARKERS):
        raise ProjectionRefusedError("PROVIDER_URL_IN_PROJECTION")
    return f"{resolver_base_url.strip().rstrip('/')}/stream/{video_id}"


def _text(value: str) -> str:
    return _XML_ILLEGAL.sub("", value)


def render_nfo(video_id: uuid.UUID, metadata: EffectiveMetadata) -> bytes:
    """A Kodi/Jellyfin movie NFO. ``lockdata`` keeps Jellyfin from replacing pixAV's values."""
    movie = ET.Element("movie")
    ET.SubElement(movie, "title").text = _text(metadata.title)
    if metadata.description:
        ET.SubElement(movie, "plot").text = _text(metadata.description)
    if metadata.release_date:
        ET.SubElement(movie, "premiered").text = metadata.release_date.isoformat()
        ET.SubElement(movie, "year").text = str(metadata.release_date.year)
    if metadata.studio:
        ET.SubElement(movie, "studio").text = _text(metadata.studio)
    for tag in metadata.tags:
        ET.SubElement(movie, "tag").text = _text(tag)
    for performer in metadata.performers:
        ET.SubElement(ET.SubElement(movie, "actor"), "name").text = _text(performer)
    ET.SubElement(movie, "uniqueid", {"type": "pixav", "default": "true"}).text = str(video_id)
    ET.SubElement(movie, "lockdata").text = "true"
    ET.indent(movie)
    return ET.tostring(movie, encoding="utf-8", xml_declaration=True) + b"\n"


def _content_file(name: str, content: bytes) -> ProjectionFile:
    return ProjectionFile(name=name, sha256=hashlib.sha256(content).hexdigest(), size=len(content), content=content)


def render_projection(
    video_id: uuid.UUID,
    metadata: EffectiveMetadata,
    artwork: Sequence[ArtworkRecord],
    *,
    resolver_base_url: str,
) -> ProjectionSpec:
    name = item_name(video_id)
    strm = f"{stream_url(resolver_base_url, video_id)}\n".encode()
    nfo = render_nfo(video_id, metadata)
    for text in (strm, nfo):
        lowered = text.decode("utf-8", "replace").lower()
        if any(marker in lowered for marker in _PROVIDER_MARKERS):
            raise ProjectionRefusedError("PROVIDER_URL_IN_PROJECTION")
    files = [_content_file(f"{name}.strm", strm), _content_file(f"{name}.nfo", nfo)]
    for record in sorted(artwork, key=lambda item: item.kind):
        files.append(
            ProjectionFile(
                name=f"{record.kind}.{EXTENSIONS[record.media_type]}", sha256=record.sha256, size=record.size_bytes
            )
        )
    return ProjectionSpec(video_id=video_id, files=tuple(files), digest=projection_digest(files))
