"""Effective metadata: provider namespaces merged under one versioned policy.

Each provider keeps its own namespace in ``videos.metadata_json`` and is read
through its own adapter, so one unreadable section is skipped without losing
the others. A person's override wins over every provider for its field, and no
scraper writes the override table. The result records, per field, where its
value came from (BDD-095, 096, 097). Only the listed fields are ever read: a
provider section may also carry local paths or file facts, and none of those
can reach a projection.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

POLICY_VERSION = "metadata-merge-v1"
OVERRIDE_FIELDS = ("title", "description", "release_date", "studio", "performers", "tags")
Source = Literal["manual", "stash", "discovery", "video"]

_TEXT_LIMIT = 500
_DESCRIPTION_LIMIT = 10_000
_NAME_LIMIT = 200
_LIST_LIMIT = 200
_WHITESPACE = re.compile(r"\s+")


def _line(value: object, limit: int = _TEXT_LIMIT) -> str | None:
    """One line of display text, or None when there is nothing to show."""
    if not isinstance(value, str):
        return None
    cleaned = _WHITESPACE.sub(" ", value).strip()
    return cleaned[:limit] or None


def _names(values: Iterable[object]) -> tuple[str, ...]:
    """Distinct non-empty names, first spelling kept, compared without case."""
    seen: dict[str, str] = {}
    for value in values:
        name = _line(value, _NAME_LIMIT)
        if name is not None and name.casefold() not in seen:
            seen[name.casefold()] = name
    return tuple(seen.values())[:_LIST_LIMIT]


def _release_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


class DiscoveryMetadata(BaseModel):
    """What the source adapter saw when it found the video."""

    model_config = {"frozen": True, "extra": "ignore"}

    title: str | None = None
    tags: tuple[str, ...] = ()


class StashMetadata(BaseModel):
    """The Stash scene match, if one was found. File facts are not read."""

    model_config = {"frozen": True, "extra": "ignore"}

    found: bool = False
    title: str | None = None
    date: str | None = None
    details: str | None = None
    studio: str | None = None
    tags: tuple[str, ...] = ()
    performers: tuple[str, ...] = ()


class ProviderMetadata(BaseModel):
    model_config = {"frozen": True}

    discovery: DiscoveryMetadata | None = None
    stash: StashMetadata | None = None


def provider_metadata(raw: object) -> ProviderMetadata:
    """Read each provider namespace on its own; an unreadable one is skipped."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            logger.warning("video metadata is not JSON; no provider metadata is used")
            return ProviderMetadata()
    if not isinstance(raw, Mapping):
        return ProviderMetadata()
    sections: dict[str, Any] = {}
    for name, model in (("discovery", DiscoveryMetadata), ("stash", StashMetadata)):
        section = raw.get(name)
        if section is None:
            continue
        try:
            sections[name] = model.model_validate(section)
        except ValidationError:
            logger.warning("ignoring unreadable %s metadata section", name)
    return ProviderMetadata(**sections)


def parse_override(field: str, value: object) -> str | list[str]:
    """Validate one manual value; the stored form is what this returns."""
    if field not in OVERRIDE_FIELDS:
        raise ValueError(f"unknown metadata field: {field}")
    if field in ("performers", "tags"):
        if not isinstance(value, list | tuple) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{field} override must be a list of names")
        return list(_names(value))
    if field == "release_date":
        parsed = _release_date(value)
        if parsed is None:
            raise ValueError("release_date override must be an ISO date (YYYY-MM-DD)")
        return parsed.isoformat()
    limit = _DESCRIPTION_LIMIT if field == "description" else _TEXT_LIMIT
    text = value.strip()[:limit] if field == "description" and isinstance(value, str) else _line(value, limit)
    if not text:
        raise ValueError(f"{field} override must be non-empty text")
    return text


class EffectiveMetadata(BaseModel):
    """The metadata a projection renders, with where each field came from."""

    model_config = {"frozen": True}

    policy: Literal["metadata-merge-v1"] = "metadata-merge-v1"
    title: str = Field(min_length=1)
    description: str | None = None
    release_date: date | None = None
    studio: str | None = None
    performers: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    provenance: dict[str, tuple[Source, ...]] = Field(default_factory=dict)

    @field_validator("provenance")
    @classmethod
    def _known_fields(cls, value: dict[str, tuple[Source, ...]]) -> dict[str, tuple[Source, ...]]:
        unknown = set(value) - set(OVERRIDE_FIELDS)
        if unknown:
            raise ValueError(f"provenance for unknown fields: {sorted(unknown)}")
        return value


def _first(candidates: Sequence[tuple[Source, Any]]) -> tuple[Any, tuple[Source, ...]]:
    for source, value in candidates:
        if value not in (None, (), ""):
            return value, (source,)
    return None, ()


def effective_metadata(
    *,
    video_title: str,
    video_tags: Iterable[str],
    providers: ProviderMetadata,
    overrides: Mapping[str, object],
) -> EffectiveMetadata:
    """Apply ``metadata-merge-v1``: manual, then Stash, then what the video row holds.

    Tags without an override are the union of every namespace, sorted without
    case so a provider reordering them does not republish anything. Performers
    keep the provider's billing order. A missing optional field stays empty; the
    title always exists because every video row has one.
    """
    manual: dict[str, object] = {}
    for field, value in overrides.items():
        try:
            manual[field] = parse_override(field, value)
        except ValueError:
            logger.warning("ignoring invalid %s override", field)
    if "release_date" in manual:
        manual["release_date"] = _release_date(manual["release_date"])
    stash = providers.stash if providers.stash is not None and providers.stash.found else None
    discovery = providers.discovery

    fields: dict[str, Any] = {}
    provenance: dict[str, tuple[Source, ...]] = {}

    def choose(name: str, candidates: Sequence[tuple[Source, Any]]) -> None:
        value, origin = _first([("manual", manual.get(name)), *candidates])
        if origin:
            fields[name], provenance[name] = value, origin

    choose("title", [("stash", _line(stash.title) if stash else None), ("video", _line(video_title))])
    choose("description", [("stash", (stash.details or "").strip()[:_DESCRIPTION_LIMIT] if stash else None)])
    choose("release_date", [("stash", _release_date(stash.date) if stash else None)])
    choose("studio", [("stash", _line(stash.studio, _NAME_LIMIT) if stash else None)])
    choose("performers", [("stash", _names(stash.performers) if stash else None)])
    if "tags" in manual:
        fields["tags"], provenance["tags"] = manual["tags"], ("manual",)
    else:
        contributions: list[tuple[Source, tuple[str, ...]]] = [
            ("video", _names(video_tags)),
            ("discovery", _names(discovery.tags) if discovery else ()),
            ("stash", _names(stash.tags) if stash else ()),
        ]
        merged = _names(tag for _, names in contributions for tag in names)
        if merged:
            fields["tags"] = tuple(sorted(merged, key=lambda tag: (tag.casefold(), tag)))
            provenance["tags"] = tuple(source for source, names in contributions if names)

    if "title" not in fields:
        raise ValueError("a video without any title cannot be published")
    return EffectiveMetadata(**fields, provenance=provenance)
