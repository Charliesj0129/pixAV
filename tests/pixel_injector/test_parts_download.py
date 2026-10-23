import hashlib
import uuid
import zipfile
from types import ModuleType
from unittest.mock import MagicMock, Mock, patch

import pytest

from pixav.media_loader.video_parts import PartMedia
from pixav.pixel_injector import parts_download
from pixav.pixel_injector.canary_download import OriginalMismatchError, extract_original
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


@pytest.mark.parametrize("artifact", ["receipt", "file", "partial", "receipt-pending"])
def test_incomplete_cloud_cache_is_preserved_before_browser_retry(tmp_path, artifact, browser_stub):
    from pixav.pixel_injector.parts_download import download_original

    part = cached_part()
    suffix = {"receipt": ".json", "file": "", "partial": ".part", "receipt-pending": ".json.pending"}[artifact]
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


@pytest.fixture
def original_browser(browser_stub):
    """The single-video action and accessible labels were verified live in Photos.

    Exercise identity and byte publication using actual files; no preview or ZIP
    result can satisfy this original-download boundary.
    """
    part = cached_part()
    browser = MagicMock(version="verified-browser-version")
    page = browser.new_page.return_value
    videos = MagicMock()
    videos.count.return_value = 1
    download_action = MagicMock()
    more_options = MagicMock()
    more_options.click.side_effect = AssertionError("album and viewer menus are both visible during navigation")

    def locate(role, *, name, **kwargs):
        if role == "link":
            assert name.match("Video - synthetic item")
            return videos
        if role == "button":
            assert any(name.fullmatch(label) for label in ("More options", "Open info"))
            return more_options
        assert role == "menuitem"
        assert name.fullmatch("Download - Shift+D") and name.fullmatch("Download")
        assert not name.fullmatch("Download all")
        return download_action

    page.get_by_role.side_effect = locate
    download = MagicMock(suggested_filename=part.filename)
    download.save_as.side_effect = lambda path: path.write_bytes(b"original")
    page.expect_download.return_value.__enter__.return_value.value = download
    context = MagicMock()
    context.__enter__.return_value.chromium.launch.return_value = browser
    browser_stub.sync_playwright.return_value = context
    return part, browser, videos, download


def test_individual_original_publishes_only_complete_verified_bytes(tmp_path, original_browser, observed_stub):
    import json

    part, browser, _, download = original_browser
    with patch("signal.alarm"):
        report = parts_download.download_original(part, tmp_path)
    assert (tmp_path / part.filename).read_bytes() == b"original"
    assert report["observed"] == observed_stub
    assert report["download_action"] == "single-video-original"
    assert report["method"] == "photos-original-browser"
    assert json.loads((tmp_path / (part.filename + ".json")).read_text()) == report
    assert part.share_url not in json.dumps(report)
    browser.close.assert_called_once()
    download.save_as.assert_called_once()


@pytest.mark.parametrize("data", [b"preview!", b"short", b"oversized"])
def test_individual_download_rejects_rendition_and_wrong_size(tmp_path, original_browser, observed_stub, data):
    part, browser, _, download = original_browser
    download.save_as.side_effect = lambda path: path.write_bytes(data)
    with patch("signal.alarm"), pytest.raises(OriginalMismatchError):
        parts_download.download_original(part, tmp_path)
    assert not (tmp_path / part.filename).exists()
    assert not (tmp_path / (part.filename + ".json")).exists()
    assert list(tmp_path.glob("browser-*/original.mp4"))[0].read_bytes() == data
    browser.close.assert_called_once()


def test_individual_download_rejects_unrelated_provider_name(tmp_path, original_browser, observed_stub):
    part, browser, _, download = original_browser
    download.suggested_filename = "unrelated.mp4"
    with patch("signal.alarm"), pytest.raises(OriginalMismatchError, match="provider identity"):
        parts_download.download_original(part, tmp_path)
    assert not (tmp_path / part.filename).exists()
    assert not (tmp_path / (part.filename + ".json")).exists()
    assert list(tmp_path.glob("browser-*/original.mp4"))
    browser.close.assert_called_once()


def test_ambiguous_album_cannot_select_an_arbitrary_video(tmp_path, original_browser, observed_stub):
    part, browser, videos, download = original_browser
    videos.count.return_value = 2
    with patch("signal.alarm"), pytest.raises(OriginalMismatchError, match="exactly one video"):
        parts_download.download_original(part, tmp_path)
    videos.click.assert_not_called()
    download.save_as.assert_not_called()
    assert not (tmp_path / part.filename).exists()
    browser.close.assert_called_once()


@pytest.mark.parametrize("fault", [None, "replacement", "in-place", "receipt-change"])
def test_cached_original_holds_media_and_receipt_until_final_observation(tmp_path, monkeypatch, browser_stub, fault):
    import json
    import os

    from pixav.pixel_injector.canary_download import OriginalWorkConflictError
    from pixav.shared.media_file import open_media_file

    part = cached_part()
    original = tmp_path / part.filename
    original.write_bytes(b"original")
    receipt = tmp_path / (part.filename + ".json")
    stored = {"sha256": part.sha256, "size": part.size_bytes, "method": "photos-original-browser"}
    receipt.write_text(json.dumps(stored))

    def observe(path):
        assert path == original
        if fault is None:
            for retained in (original, receipt):
                with pytest.raises(BlockingIOError), open_media_file(retained, exclusive=True):
                    pytest.fail("cached read-back released a session input early")
        elif fault == "replacement":
            original.rename(tmp_path / "retained-original.mp4")
            original.write_bytes(b"original")
        elif fault == "in-place":
            before = original.stat()
            original.write_bytes(b"original")
            os.utime(original, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
        else:
            receipt.write_text(json.dumps({**stored, "browser": "changed evidence"}))
        return OBSERVED

    monkeypatch.setattr(parts_download, "observed_media", observe)
    if fault:
        with pytest.raises(OriginalWorkConflictError):
            parts_download.download_original(part, tmp_path)
    else:
        assert parts_download.download_original(part, tmp_path) == {**stored, "observed": OBSERVED}
    browser_stub.sync_playwright.assert_not_called()
    assert original.read_bytes() == b"original"
    assert receipt.exists()


@pytest.mark.parametrize("fault", [None, "replacement"])
def test_fresh_original_stays_locked_until_receipt_publication(tmp_path, monkeypatch, original_browser, fault):
    import json

    from pixav.pixel_injector.canary_download import OriginalWorkConflictError
    from pixav.shared.media_file import open_media_file

    part, _, _, _ = original_browser
    original = tmp_path / part.filename
    receipt = tmp_path / (part.filename + ".json")

    def locked():
        with pytest.raises(BlockingIOError), open_media_file(original, exclusive=True):
            pytest.fail("new read-back released the original before its receipt")

    def observe(path):
        assert path == original
        if fault:
            original.rename(tmp_path / "retained-original.mp4")
            original.write_bytes(b"original")
        else:
            locked()
        return OBSERVED

    dump = json.dump

    def commit(*args, **kwargs):
        locked()
        return dump(*args, **kwargs)

    monkeypatch.setattr(parts_download, "observed_media", observe)
    monkeypatch.setattr(parts_download.json, "dump", commit)
    with patch("signal.alarm"):
        if fault:
            with pytest.raises(OriginalWorkConflictError):
                parts_download.download_original(part, tmp_path)
            assert not receipt.exists()
            assert list(tmp_path.glob("browser-*/original.mp4"))[0].read_bytes() == b"original"
        else:
            report = parts_download.download_original(part, tmp_path)
            assert json.loads(receipt.read_text()) == report
            assert receipt.stat().st_mode & 0o777 == 0o600


def test_interrupted_original_receipt_retains_pending_and_browser_work(
    tmp_path, monkeypatch, original_browser, observed_stub
):
    part, _, _, _ = original_browser

    def interrupted(report, handle):
        handle.write('{"partial":')
        raise RuntimeError("synthetic interrupted receipt")

    monkeypatch.setattr(parts_download.json, "dump", interrupted)
    with patch("signal.alarm"), pytest.raises(RuntimeError, match="interrupted receipt"):
        parts_download.download_original(part, tmp_path)
    assert not (tmp_path / (part.filename + ".json")).exists()
    pending = tmp_path / (part.filename + ".json.pending")
    assert pending.read_text() == '{"partial":'
    assert pending.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / part.filename).read_bytes() == b"original"
    assert list(tmp_path.glob("browser-*/original.mp4"))[0].read_bytes() == b"original"


def test_original_change_during_receipt_write_cannot_publish_success(
    tmp_path, monkeypatch, original_browser, observed_stub
):
    import json

    from pixav.pixel_injector.canary_download import OriginalWorkConflictError

    part, _, _, _ = original_browser
    original = tmp_path / part.filename
    dump = json.dump

    def replace(report, handle):
        dump(report, handle)
        original.rename(tmp_path / "retained-original.mp4")
        original.write_bytes(b"original")

    monkeypatch.setattr(parts_download.json, "dump", replace)
    with patch("signal.alarm"), pytest.raises(OriginalWorkConflictError):
        parts_download.download_original(part, tmp_path)
    assert not (tmp_path / (part.filename + ".json")).exists()
    assert (tmp_path / (part.filename + ".json.pending")).exists()
    assert original.read_bytes() == b"original"
    assert list(tmp_path.glob("browser-*/original.mp4"))


@pytest.mark.parametrize("cached", [False, True])
def test_original_media_observation_must_match_the_locked_manifest(
    tmp_path, monkeypatch, original_browser, browser_stub, cached
):
    import json

    from pixav.pixel_injector.canary_download import OriginalWorkConflictError

    part, _, _, _ = original_browser
    if cached:
        (tmp_path / part.filename).write_bytes(b"original")
        (tmp_path / (part.filename + ".json")).write_text(
            json.dumps({"sha256": part.sha256, "size": part.size_bytes, "method": "photos-original-browser"})
        )
    monkeypatch.setattr(parts_download, "observed_media", lambda path: {**OBSERVED, "sha256": "0" * 64})
    with patch("signal.alarm"), pytest.raises(OriginalWorkConflictError):
        parts_download.download_original(part, tmp_path)
    if cached:
        browser_stub.sync_playwright.assert_not_called()
    else:
        assert not (tmp_path / (part.filename + ".json")).exists()
