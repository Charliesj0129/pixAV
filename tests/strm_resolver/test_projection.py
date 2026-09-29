"""Projection content and atomic pointer contracts using synthetic artwork."""

import uuid
import xml.etree.ElementTree as ET

import pytest
from PIL import Image

from pixav.strm_resolver.projection import LibraryProjection, effective_metadata, nfo, stable_url


def test_manual_values_win_and_nfo_is_escaped():
    values = {
        "providers": {"fixture": {"title": "scraped", "description": "A & B", "performers": ["Synthetic Person"]}},
        "manual_overrides": {"title": "Manual <title>", "tags": ["synthetic"]},
    }
    effective = effective_metadata("base", values)
    parsed = ET.fromstring(nfo(effective))  # noqa: S314 -- locally generated synthetic XML
    assert parsed.findtext("title") == "Manual <title>"
    assert parsed.findtext("plot") == "A & B"
    assert parsed.findtext("actor/name") == "Synthetic Person"
    assert parsed.findtext("tag") == "synthetic"
    assert values["providers"]["fixture"]["title"] == "scraped"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.test",
        "https://example.test/?token=secret",
        "file:///tmp",
        "https://example.test/#secret",
    ],
)
def test_projection_rejects_credential_bearing_urls(url):
    with pytest.raises(ValueError):
        stable_url(url, uuid.uuid4())


def test_pointer_switch_only_exposes_complete_version(tmp_path):
    projection = LibraryProjection(
        None, root=tmp_path / "library", artwork_root=tmp_path, base_url="https://example.test"
    )
    projection._directories()
    video = uuid.uuid4()
    files = {"movie.strm": b"synthetic", "movie.nfo": b"<movie/>", "poster.png": b"image"}
    one = projection._version(video, "one", files)
    active = projection._activate(video, one)
    two = projection._version(video, "two", {**files, "movie.nfo": b"<movie><title>new</title></movie>"})
    assert (active / "movie.nfo").read_bytes() == b"<movie/>"
    projection._activate(video, two)
    assert len(list(active.iterdir())) == 3
    assert (one / "movie.nfo").read_bytes() == b"<movie/>"


def test_retained_artwork_is_validated_and_symlinks_rejected(tmp_path):
    projection = LibraryProjection(
        None, root=tmp_path / "library", artwork_root=tmp_path, base_url="https://example.test"
    )
    poster = tmp_path / "poster.png"
    Image.new("RGB", (8, 8), "blue").save(poster)
    data, suffix, digest = projection._poster(poster)
    assert data == poster.read_bytes() and suffix == "png" and len(digest) == 64
    link = tmp_path / "link.png"
    link.symlink_to(poster)
    with pytest.raises(ValueError):
        projection._poster(link)
    poster.write_bytes(b"invalid image")
    with pytest.raises(OSError):
        projection._poster(poster)
