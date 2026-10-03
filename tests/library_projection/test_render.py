"""Projection rendering (BDD-087-094, 098, 099)."""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET  # noqa: S405 - parses output this module rendered
from datetime import date

import pytest

from pixav.library_projection.metadata import EffectiveMetadata
from pixav.library_projection.render import ProjectionRefusedError, render_nfo, render_projection, stream_url
from pixav.library_projection.repository import ArtworkRecord

VIDEO = uuid.UUID("88888888-8888-4888-8888-888888888888")
BASE = "http://resolver.lan:8000"
FULL = EffectiveMetadata(
    title="Synthetic <Title> & Co",
    description="Plot line one.\nPlot line two.",
    release_date=date(2021, 3, 4),
    studio="Synthetic Studio",
    performers=("Performer B", "Performer A"),
    tags=("4k", "drama"),
    provenance={"title": ("manual",)},
)


def poster(kind: str = "poster", media_type: str = "image/jpeg") -> ArtworkRecord:
    return ArtworkRecord(
        id=uuid.uuid4(), video_id=VIDEO, kind=kind, sha256="c" * 64, size_bytes=1234, media_type=media_type
    )


def parsed(metadata: EffectiveMetadata) -> ET.Element:
    return ET.fromstring(render_nfo(VIDEO, metadata))  # noqa: S314 - our own rendered bytes


def files(spec) -> dict:
    return {file.name: file for file in spec.files}


def test_strm_holds_only_the_stable_url_bdd_087_088():
    spec = render_projection(VIDEO, FULL, [], resolver_base_url=BASE + "/")

    strm = files(spec)[f"pixav-{VIDEO}.strm"].content
    assert strm == f"{BASE}/stream/{VIDEO}\n".encode()


@pytest.mark.parametrize(
    ("base", "code"),
    [
        ("http://user:secret@resolver.lan", "RESOLVER_BASE_URL_INVALID"),
        ("http://resolver.lan/?token=abc", "RESOLVER_BASE_URL_INVALID"),
        ("file:///srv/media", "RESOLVER_BASE_URL_INVALID"),
        ("https://lh3.googleusercontent.com", "PROVIDER_URL_IN_PROJECTION"),
    ],
)
def test_a_base_url_that_could_leak_is_refused_bdd_088(base, code):
    with pytest.raises(ProjectionRefusedError) as refused:
        stream_url(base, VIDEO)
    assert refused.value.code == code


def test_metadata_mentioning_a_provider_url_is_never_written_bdd_088():
    leaking = FULL.model_copy(update={"description": "see https://photos.app.goo.gl/abc"})

    with pytest.raises(ProjectionRefusedError, match="PROVIDER_URL_IN_PROJECTION"):
        render_projection(VIDEO, leaking, [], resolver_base_url=BASE)


def test_nfo_carries_every_known_field_bdd_090_091_092_093():
    movie = parsed(FULL)

    assert movie.findtext("title") == "Synthetic <Title> & Co"
    assert movie.findtext("plot") == "Plot line one.\nPlot line two."
    assert movie.findtext("premiered") == "2021-03-04" and movie.findtext("year") == "2021"
    assert movie.findtext("studio") == "Synthetic Studio"
    assert [tag.text for tag in movie.findall("tag")] == ["4k", "drama"]
    assert [actor.findtext("name") for actor in movie.findall("actor")] == ["Performer B", "Performer A"]
    assert movie.find("uniqueid").attrib == {"type": "pixav", "default": "true"}
    assert movie.findtext("uniqueid") == str(VIDEO) and movie.findtext("lockdata") == "true"


def test_missing_optional_metadata_still_renders_bdd_094():
    movie = parsed(EffectiveMetadata(title="Only A Title"))

    assert movie.findtext("title") == "Only A Title"
    for absent in ("plot", "premiered", "studio", "tag", "actor"):
        assert movie.find(absent) is None


def test_characters_xml_cannot_carry_are_dropped():
    movie = parsed(EffectiveMetadata(title="Bell\x07 and form\x0cfeed"))

    assert movie.findtext("title") == "Bell and formfeed"


def test_names_depend_on_identity_only_so_a_title_change_is_the_same_item_bdd_089():
    before = render_projection(VIDEO, FULL, [], resolver_base_url=BASE)
    after = render_projection(VIDEO, FULL.model_copy(update={"title": "Renamed"}), [], resolver_base_url=BASE)

    assert sorted(files(before)) == sorted(files(after)) == [f"pixav-{VIDEO}.nfo", f"pixav-{VIDEO}.strm"]
    assert before.digest != after.digest


def test_rendering_is_deterministic():
    first = render_projection(VIDEO, FULL, [poster()], resolver_base_url=BASE)
    second = render_projection(VIDEO, FULL, [poster()], resolver_base_url=BASE)

    assert first == second


def test_retained_artwork_is_referenced_by_digest_bdd_098_099():
    spec = render_projection(
        VIDEO, FULL, [poster("backdrop", "image/webp"), poster("poster", "image/png")], resolver_base_url=BASE
    )

    artwork = files(spec)
    assert artwork["poster.png"].sha256 == "c" * 64 and artwork["poster.png"].content is None
    assert artwork["backdrop.webp"].size == 1234
    without_backdrop = render_projection(VIDEO, FULL, [poster("poster", "image/png")], resolver_base_url=BASE)
    assert "backdrop.webp" not in files(without_backdrop)
