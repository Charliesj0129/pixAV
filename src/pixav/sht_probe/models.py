"""Structured discovery results with list compatibility for existing callers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class MagnetCandidate:
    uri: str
    title: str
    source_url: str


class CrawlResult(list[str]):
    """New magnet list plus observability counts for a crawl cycle."""

    def __init__(
        self,
        new_magnets: Iterable[str] = (),
        *,
        thread_links: int = 0,
        extracted_magnets: int = 0,
        inserted: int | None = None,
        untitled_rejected: int = 0,
    ) -> None:
        super().__init__(new_magnets)
        self.thread_links = thread_links
        self.extracted_magnets = extracted_magnets
        self.inserted = len(self) if inserted is None else inserted
        self.untitled_rejected = untitled_rejected

    @property
    def new_magnets(self) -> list[str]:
        return list(self)
