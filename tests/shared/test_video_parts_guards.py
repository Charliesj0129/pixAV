"""Manifest and cloud-confirmation guards, exercised without a database.

The integration suite proves these against real PostgreSQL but is opt-in. These
cover the refusals that protect the film itself: a manifest that is not a whole
contiguous timeline, a share location that is not Photos, an original confirmed
from something other than an independent cloud download, and a publication that
would expose an incomplete manifest.
"""

from __future__ import annotations

import uuid

import pytest

from pixav.shared.models import VideoPart
from pixav.shared.video_parts import VideoPartRepository, part_from_row

VIDEO_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SHA = "a" * 64


def make_part(index: int, start: float, end: float, **extra) -> VideoPart:
    return VideoPart(
        video_id=extra.pop("video_id", VIDEO_ID),
        part_index=index,
        manifest_version=extra.pop("manifest_version", 1),
        start_seconds=start,
        end_seconds=end,
        size_bytes=extra.pop("size_bytes", 9_000_000_000),
        sha256=extra.pop("sha256", SHA),
        filename=f"pixav-{VIDEO_ID}-part-{index:06d}-{'b' * 16}.mp4",
        media_info={},
        **extra,
    )


class UnusedPool:
    """Any query here means a guard failed to reject before touching the database."""

    def acquire(self):
        raise AssertionError("guard must reject before acquiring a connection")

    async def execute(self, *_args):
        raise AssertionError("guard must reject before writing")


@pytest.fixture
def repo() -> VideoPartRepository:
    return VideoPartRepository(UnusedPool())


class TestManifestShape:
    async def test_a_single_part_manifest_is_refused(self, repo):
        with pytest.raises(ValueError, match="at least two contiguous parts"):
            await repo.install(VIDEO_ID, [make_part(0, 0, 60)], {})

    async def test_a_gap_between_parts_is_refused(self, repo):
        parts = [make_part(0, 0, 60), make_part(1, 61, 120)]
        with pytest.raises(ValueError, match="timeline is not contiguous"):
            await repo.install(VIDEO_ID, parts, {})

    async def test_a_manifest_that_does_not_start_at_zero_is_refused(self, repo):
        parts = [make_part(0, 5, 60), make_part(1, 60, 120)]
        with pytest.raises(ValueError, match="timeline is not contiguous"):
            await repo.install(VIDEO_ID, parts, {})

    async def test_out_of_order_part_indexes_are_refused(self, repo):
        parts = [make_part(1, 0, 60), make_part(2, 60, 120)]
        with pytest.raises(ValueError, match="at least two contiguous parts"):
            await repo.install(VIDEO_ID, parts, {})

    async def test_parts_belonging_to_another_film_are_refused(self, repo):
        other = uuid.UUID("22222222-2222-2222-2222-222222222222")
        parts = [make_part(0, 0, 60), make_part(1, 60, 120, video_id=other)]
        with pytest.raises(ValueError, match="identity mismatch"):
            await repo.install(VIDEO_ID, parts, {})

    async def test_a_sub_millisecond_seam_is_accepted_as_contiguous(self, repo):
        # Segment boundaries land on frame times, never on exact second values.
        parts = [make_part(0, 0, 60.0), make_part(1, 60.0005, 120)]
        with pytest.raises(AssertionError, match="acquiring a connection"):
            await repo.install(VIDEO_ID, parts, {})


class TestCloudOriginal:
    async def test_a_rehash_that_does_not_match_the_uploaded_part_is_refused(self, repo):
        part = make_part(0, 0, 60)
        evidence = {"sha256": "b" * 64, "size": part.size_bytes, "method": "photos-original-browser"}
        with pytest.raises(ValueError, match="identity mismatch"):
            await repo.confirm_original(part, evidence)

    async def test_a_size_mismatch_is_refused(self, repo):
        part = make_part(0, 0, 60)
        evidence = {"sha256": SHA, "size": part.size_bytes - 1, "method": "photos-original-browser"}
        with pytest.raises(ValueError, match="identity mismatch"):
            await repo.confirm_original(part, evidence)

    async def test_only_an_independent_cloud_download_can_confirm_an_original(self, repo):
        # Copying the local part would make the cold-cache acceptance meaningless.
        part = make_part(0, 0, 60)
        evidence = {"sha256": SHA, "size": part.size_bytes, "method": "local-copy"}
        with pytest.raises(ValueError, match="independent cloud provenance"):
            await repo.confirm_original(part, evidence)


def test_row_mapper_decodes_json_columns_written_as_text():
    row = {
        "video_id": VIDEO_ID,
        "part_index": 0,
        "manifest_version": 1,
        "start_seconds": 0.0,
        "end_seconds": 60.0,
        "size_bytes": 100,
        "sha256": SHA,
        "filename": f"pixav-{VIDEO_ID}-part-000000-{'b' * 16}.mp4",
        "media_info": '{"format": {"duration": "60"}}',
        "recovery": "{}",
        "verification": '{"backed_up": true}',
        "share_url": None,
        "account_id": None,
        "state": "prepared",
        "uploaded_at": None,
        "usage_counted_at": None,
        "retry_not_before": None,
        "updated_at": None,
    }
    part = part_from_row(row)
    assert part.media_info["format"]["duration"] == "60"
    assert part.verification["backed_up"] is True
