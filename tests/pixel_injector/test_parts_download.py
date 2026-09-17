import hashlib
import uuid
import zipfile
from types import ModuleType
from unittest.mock import Mock, patch

import pytest

from pixav.media_loader.video_parts import PartMedia
from pixav.pixel_injector import parts_download
from pixav.pixel_injector.canary_download import extract_original
from pixav.pixel_injector.parts_download import prepare, quarantine
from pixav.shared.models import VideoPart

# What ffprobe would report for the retrieved bytes. Verification needs to know
# what media came back, not only that the byte count and hash matched.
OBSERVED = {
    "container": "mov,mp4",
    "size_bytes": 8,
    "duration_seconds": 10.0,
    "sha256": hashlib.sha256(b"original").hexdigest(),
    "streams": [
        {"kind": "video", "codec": "h264", "width": 1920, "height": 1080},
        {"kind": "audio", "codec": "aac", "width": 0, "height": 0},
    ],
}


@pytest.fixture
def observed_stub(monkeypatch):
    """ffprobe lives in the tools image; these tests exercise the cache boundary."""
    monkeypatch.setattr(parts_download, "observed_media", lambda path: OBSERVED)
    return OBSERVED


def test_forced_zip64_original(tmp_path):
    data = b"cloud-original" * 1024
    archive = tmp_path / "original.zip"
    with zipfile.ZipFile(archive, "w", allowZip64=True) as bundle:
        with bundle.open("part.mp4", "w", force_zip64=True) as item:
            item.write(data)
    destination = tmp_path / "part.mp4"
    extract_original(archive, destination, "part.mp4", len(data), hashlib.sha256(data).hexdigest())
    assert destination.read_bytes() == data


def test_symlink_zip_member_rejected(tmp_path):
    info = zipfile.ZipInfo("part.mp4")
    info.create_system = 3
    info.external_attr = 0o120777 << 16
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(info, b"target")
    with pytest.raises(ValueError, match="expected item"):
        extract_original(archive, tmp_path / "out.mp4", "part.mp4", 6, hashlib.sha256(b"target").hexdigest())


def test_cold_merge_uses_only_verified_cloud_paths_and_atomic_publication(tmp_path):
    video = uuid.uuid4()
    digest = hashlib.sha256(b"original").hexdigest()
    parts = [
        VideoPart(
            video_id=video,
            part_index=i,
            manifest_version=1,
            start_seconds=i * 10,
            end_seconds=(i + 1) * 10,
            filename=f"pixav-{video}-part-{i:06d}-{digest[:16]}.mp4",
            size_bytes=8,
            sha256=digest,
        )
        for i in range(2)
    ]
    calls = []

    def download(part, root):
        (root / part.filename).write_bytes(b"original")
        return {"sha256": part.sha256, "size": 8, "method": "photos-original-browser"}

    def merge(paths, destination, duration, **kwargs):
        calls.extend(paths)
        assert all(path.parent == tmp_path / "cloud" for path in paths)
        destination.write_bytes(b"verified merge")

    with (
        patch("pixav.pixel_injector.parts_download.download_original", side_effect=download),
        patch.object(PartMedia, "merge", side_effect=merge),
        patch.object(PartMedia, "fingerprint", return_value={}),
        patch.object(PartMedia, "compare"),
    ):
        report = prepare({"parts": [p.model_dump(mode="json") for p in parts], "reference": {"duration": 20}}, tmp_path)
    assert (tmp_path / report["filename"]).read_bytes() == b"verified merge"
    assert report["cold_inputs"] == "photos-only"
    assert calls == [tmp_path / "cloud" / p.filename for p in parts]


@pytest.mark.parametrize("indices", [[0], [0, 2], [1, 0]])
def test_incomplete_or_out_of_order_manifest_fails_before_downloading(tmp_path, indices):
    video = uuid.uuid4()
    parts = [
        VideoPart(
            video_id=video,
            part_index=i,
            manifest_version=1,
            start_seconds=i * 10,
            end_seconds=(i + 1) * 10,
            filename=f"pixav-{video}-part-{i:06d}-{'a' * 16}.mp4",
            size_bytes=8,
            sha256="a" * 64,
        )
        for i in indices
    ]
    with patch("pixav.pixel_injector.parts_download.download_original") as download:
        with pytest.raises(ValueError, match="manifest"):
            prepare({"parts": [p.model_dump(mode="json") for p in parts]}, tmp_path)
    download.assert_not_called()


class TestQuarantine:
    """A crash between publishing bytes and writing the receipt must be recoverable.

    Bytes with no receipt cannot be trusted and cannot be explained. Leaving them
    in place blocks every later attempt, and deleting them destroys the only
    evidence, so they are moved aside and the part is fetched again.
    """

    def test_artifacts_move_aside_and_free_the_name(self, tmp_path):
        destination = tmp_path / "part.mp4"
        stray = tmp_path / "part.mp4.part"
        destination.write_bytes(b"untrusted")
        stray.write_bytes(b"half")
        holding = quarantine(tmp_path, destination, tmp_path / "part.mp4.json", stray)
        assert not destination.exists()
        assert not stray.exists()
        assert (holding / "part.mp4").read_bytes() == b"untrusted"
        assert (holding / "part.mp4.part").read_bytes() == b"half"

    def test_nothing_is_destroyed_and_missing_paths_are_skipped(self, tmp_path):
        holding = quarantine(tmp_path, tmp_path / "absent.mp4")
        assert holding.is_dir()
        assert list(holding.iterdir()) == []

    def test_each_quarantine_keeps_its_own_folder(self, tmp_path):
        first = tmp_path / "a.mp4"
        first.write_bytes(b"one")
        one = quarantine(tmp_path, first)
        first.write_bytes(b"two")
        two = quarantine(tmp_path, first)
        assert one != two
        assert (one / "a.mp4").read_bytes() == b"one"
        assert (two / "a.mp4").read_bytes() == b"two"


def cached_part():
    video = uuid.uuid4()
    data = b"original"
    digest = hashlib.sha256(data).hexdigest()
    return VideoPart(
        video_id=video,
        part_index=0,
        manifest_version=1,
        start_seconds=0,
        end_seconds=10,
        filename=f"pixav-{video}-part-000000-{digest[:16]}.mp4",
        size_bytes=len(data),
        sha256=digest,
        share_url="https://photos.google.com/test",
    )


@pytest.mark.parametrize("artifact", ["receipt", "file", "partial"])
def test_incomplete_cloud_cache_is_preserved_before_browser_retry(tmp_path, artifact, browser_stub):
    from pixav.pixel_injector.parts_download import download_original

    part = cached_part()
    suffix = {"receipt": ".json", "file": "", "partial": ".part"}[artifact]
    original = tmp_path / (part.filename + suffix)
    original.write_bytes(b"incomplete evidence")
    with patch.object(browser_stub, "sync_playwright", side_effect=RuntimeError("browser retry reached")):
        # Stop before network; the real recovery path must already have preserved evidence.
        with patch("signal.alarm"):
            with pytest.raises(RuntimeError, match="browser retry reached"):
                download_original(part, tmp_path)
    assert not original.exists()
    held = list((tmp_path / "quarantine").glob("*/" + original.name))
    assert len(held) == 1
    assert held[0].read_bytes() == b"incomplete evidence"


@pytest.mark.parametrize("receipt_size", [8, 9])
def test_cache_reuse_requires_matching_receipt_size(tmp_path, receipt_size, browser_stub, observed_stub):
    import json

    from pixav.pixel_injector.parts_download import download_original

    part = cached_part()
    (tmp_path / part.filename).write_bytes(b"original")
    receipt = {"sha256": part.sha256, "size": receipt_size, "method": "photos-original-browser"}
    (tmp_path / (part.filename + ".json")).write_text(json.dumps(receipt))
    with patch.object(browser_stub, "sync_playwright") as browser:
        if receipt_size == part.size_bytes:
            assert download_original(part, tmp_path) == {**receipt, "observed": observed_stub}
        else:
            with pytest.raises(ValueError, match="corrupt"):
                download_original(part, tmp_path)
        browser.assert_not_called()


def test_a_reused_cache_still_reports_what_the_media_is_bdd_053(tmp_path, browser_stub, observed_stub):
    """An old receipt describes bytes; the media facts are recomputed from them.

    A receipt written before the observation was part of the contract still
    points at bytes that are present and still hash correctly, so the honest
    answer is to look at them again rather than fetch the segment twice.
    """
    import json

    from pixav.pixel_injector.parts_download import download_original

    part = cached_part()
    (tmp_path / part.filename).write_bytes(b"original")
    stored = {"sha256": part.sha256, "size": part.size_bytes, "method": "photos-original-browser"}
    (tmp_path / (part.filename + ".json")).write_text(json.dumps(stored))

    with patch.object(browser_stub, "sync_playwright") as browser:
        report = download_original(part, tmp_path)

    browser.assert_not_called()
    assert report["observed"] == observed_stub
    assert "local_path" not in report and part.share_url not in json.dumps(report)


@pytest.fixture
def browser_stub(monkeypatch):
    # Browser dependencies live in the tools image; unit tests exercise only the cache boundary.
    module = ModuleType("playwright.sync_api")
    module.sync_playwright = Mock()
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", module)
    return module
