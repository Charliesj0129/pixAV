"""Ordering rules for the migration runner.

`--until` exists because 008 (create source_candidates) is safe to apply while
the deployed code still runs, and 009 (drop videos.cdn_url) is not. Getting the
stop point wrong is a production outage, so the selection logic is a pure
function with tests rather than a loop buried in the connection handling.
"""

from __future__ import annotations

import pytest

from scripts.migrate import select_pending

FILES = [
    "007_unattended_pipeline.sql",
    "008_source_candidates.sql",
    "009_drop_video_cdn_url.sql",
]


class TestSelectPending:
    def test_applies_everything_pending_in_filename_order(self) -> None:
        assert select_pending(FILES, applied=set()) == FILES

    def test_input_order_does_not_decide_apply_order(self) -> None:
        """Directory listing order is arbitrary; migration order is not."""
        assert select_pending(list(reversed(FILES)), applied=set()) == FILES

    def test_skips_already_applied(self) -> None:
        applied = {"007_unattended_pipeline.sql"}

        assert select_pending(FILES, applied) == FILES[1:]

    def test_until_stops_after_the_named_migration(self) -> None:
        """The expand half alone: 008 applies, 009 stays pending."""
        assert select_pending(FILES, applied=set(), until="008_source_candidates.sql") == [
            "007_unattended_pipeline.sql",
            "008_source_candidates.sql",
        ]

    def test_until_is_inclusive_of_its_own_file(self) -> None:
        applied = {"007_unattended_pipeline.sql"}

        selected = select_pending(FILES, applied, until="008_source_candidates.sql")

        assert selected == ["008_source_candidates.sql"]

    def test_until_already_applied_selects_nothing(self) -> None:
        applied = {"007_unattended_pipeline.sql", "008_source_candidates.sql"}

        assert select_pending(FILES, applied, until="008_source_candidates.sql") == []

    def test_unknown_until_is_refused_rather_than_ignored(self) -> None:
        """A typo must not silently fall through to applying every migration."""
        with pytest.raises(ValueError, match="names no migration"):
            select_pending(FILES, applied=set(), until="008_source_candidate.sql")
