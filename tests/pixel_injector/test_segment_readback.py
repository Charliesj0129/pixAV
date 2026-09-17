"""The cold read-back process: provider bytes in, an integrity receipt out."""

from __future__ import annotations

import json
import uuid

import pytest

from pixav.pixel_injector import segment_readback
from pixav.shared.storage_models import RemoteAssetSegment

ASSET = uuid.UUID("55555555-5555-4555-8555-555555555555")
DIGEST = "a" * 64
SHARE_URL = "https://photos.app.goo.gl/managedcanary"


def segment(**overrides) -> RemoteAssetSegment:
    fields = {
        "asset_id": ASSET,
        "segment_index": 0,
        "start_seconds": 0.0,
        "end_seconds": 100.0,
        "size_bytes": 128_743_122,
        "sha256": DIGEST,
        "local_path": "",
        "share_url": SHARE_URL,
        **overrides,
    }
    return RemoteAssetSegment(**fields)


def test_the_manifest_is_parsed_and_the_provider_is_the_only_source(monkeypatch, tmp_path):
    seen: dict = {}

    def fake_download(part, root):
        seen["share_url"] = part.share_url
        seen["root"] = root
        return {"method": "photos-original-browser", "size": part.size_bytes, "sha256": part.sha256}

    monkeypatch.setattr(segment_readback, "download_original", fake_download)

    receipt = segment_readback.read_back({"version": 1, "segment": json.loads(segment().model_dump_json())}, tmp_path)

    assert seen == {"share_url": SHARE_URL, "root": tmp_path}
    assert receipt["cold_inputs"] == "provider-only"
    assert receipt["sha256"] == DIGEST


def test_a_segment_without_a_share_location_is_refused(tmp_path):
    manifest = {"version": 1, "segment": json.loads(segment(share_url=None).model_dump_json())}

    with pytest.raises(ValueError, match="share location"):
        segment_readback.read_back(manifest, tmp_path)


def test_a_symlinked_work_root_is_refused(tmp_path):
    """The read-back root must be the directory the parent created for it."""
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "work"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="real directory"):
        segment_readback.read_back({"version": 1, "segment": json.loads(segment().model_dump_json())}, link)


def test_a_failure_reports_only_the_exception_type(monkeypatch, capsys):
    """An exception message could carry the share location or a filesystem path."""

    def explode(_manifest, _root):
        raise RuntimeError(f"failed fetching {SHARE_URL} into /work/secret")

    monkeypatch.setattr(segment_readback, "read_back", explode)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({"version": 1, "segment": {}})))

    code = segment_readback.main()

    captured = capsys.readouterr().out
    assert code == 2
    assert json.loads(captured) == {"status": "BLOCKED", "error_type": "RuntimeError"}
    assert SHARE_URL not in captured
    assert "/work/secret" not in captured


class _Stdin:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def read(self, *_args) -> str:
        return self._payload
