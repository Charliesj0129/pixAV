"""Effective metadata policy metadata-merge-v1 (BDD-090-097)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from pixav.library_projection.metadata import (
    EffectiveMetadata,
    effective_metadata,
    parse_override,
    provider_metadata,
)

STASH = {
    "found": True,
    "stash_id": "42",
    "title": "Scraped Title",
    "date": "2021-03-04",
    "details": "A synthetic plot.\nSecond line.",
    "studio": "Synthetic Studio",
    "tags": ["Drama", "night"],
    "performers": ["Performer B", "Performer A", "performer b"],
    "file_info": {"path": "/data/downloads/private/file.mp4", "size": 1},
}
DISCOVERY = {"source_url": "https://example.invalid/thread", "title": "raw release name", "tags": ["NIGHT", "4k"]}


def merge(metadata: object, *, overrides: dict | None = None, title: str = "Row Title", tags=("drama",)):
    return effective_metadata(
        video_title=title, video_tags=tags, providers=provider_metadata(metadata), overrides=overrides or {}
    )


def test_stash_fills_known_fields_with_provenance_bdd_090_092_093_097():
    result = merge({"discovery": DISCOVERY, "stash": STASH})

    assert result.title == "Scraped Title"
    assert result.description == "A synthetic plot.\nSecond line."
    assert result.release_date == date(2021, 3, 4)
    assert result.studio == "Synthetic Studio"
    assert result.performers == ("Performer B", "Performer A")
    assert result.provenance["title"] == ("stash",) and result.provenance["studio"] == ("stash",)


def test_tags_are_the_union_of_every_namespace_in_a_stable_order_bdd_091():
    result = merge({"discovery": DISCOVERY, "stash": STASH})
    reordered = merge(
        {"discovery": {**DISCOVERY, "tags": ["4k", "NIGHT"]}, "stash": {**STASH, "tags": ["night", "Drama"]}}
    )

    assert result.tags == ("4k", "drama", "NIGHT")
    assert reordered.tags == result.tags, "a provider reordering its tags must not change the projection"
    assert result.provenance["tags"] == ("video", "discovery", "stash")


def test_manual_override_beats_the_scraper_bdd_095():
    result = merge({"stash": STASH}, overrides={"title": "Manual Title", "tags": ["curated"]})

    assert result.title == "Manual Title" and result.provenance["title"] == ("manual",)
    assert result.tags == ("curated",) and result.provenance["tags"] == ("manual",)


def test_refreshed_scraper_value_does_not_erase_an_override_bdd_096():
    overrides = {"studio": "Manual Studio"}
    before = merge({"stash": STASH}, overrides=overrides)
    after = merge({"stash": {**STASH, "studio": "Refreshed Studio"}}, overrides=overrides)

    assert before.studio == after.studio == "Manual Studio"


def test_missing_optional_metadata_still_yields_a_title_bdd_094():
    result = merge({}, tags=())

    assert result == EffectiveMetadata(title="Row Title", provenance={"title": ("video",)})


def test_an_unmatched_stash_search_is_not_metadata():
    result = merge({"stash": {"found": False, "title": "the query text"}})

    assert result.title == "Row Title" and result.studio is None


def test_an_unreadable_section_is_skipped_without_losing_the_others():
    result = merge({"stash": {**STASH, "tags": "not-a-list"}, "discovery": DISCOVERY})

    assert result.title == "Row Title" and result.studio is None
    assert "NIGHT" in result.tags


def test_provider_file_facts_never_reach_effective_metadata():
    dumped = merge({"stash": STASH, "discovery": DISCOVERY}).model_dump_json()

    assert "/data/" not in dumped and "example.invalid" not in dumped


@pytest.mark.parametrize("raw", ["not json", "[]", None, json.dumps({"stash": STASH})])
def test_metadata_column_in_any_shape_is_read_safely(raw):
    assert merge(raw).title in ("Row Title", "Scraped Title")


def test_invalid_stash_date_is_left_out():
    assert merge({"stash": {**STASH, "date": "sometime"}}).release_date is None


def test_invalid_stored_override_is_ignored_not_fatal():
    result = merge({"stash": STASH}, overrides={"release_date": "not a date", "studio": "  "})

    assert result.release_date == date(2021, 3, 4) and result.studio == "Synthetic Studio"


@pytest.mark.parametrize(
    ("field", "value", "stored"),
    [
        ("title", "  Spaced   Title ", "Spaced Title"),
        ("release_date", "2020-01-02", "2020-01-02"),
        ("tags", ["a", "A", " b "], ["a", "b"]),
        ("performers", [], []),
        ("description", "Line one\nLine two", "Line one\nLine two"),
    ],
)
def test_override_values_are_normalised(field, value, stored):
    assert parse_override(field, value) == stored


@pytest.mark.parametrize(
    ("field", "value"),
    [("rating", "5"), ("title", ""), ("tags", "comma,separated"), ("release_date", "03/04/2021"), ("studio", 7)],
)
def test_override_values_are_validated(field, value):
    with pytest.raises(ValueError):
        parse_override(field, value)


def test_an_empty_performer_override_means_none():
    result = merge({"stash": STASH}, overrides={"performers": []})

    assert result.performers == () and result.provenance["performers"] == ("manual",)
