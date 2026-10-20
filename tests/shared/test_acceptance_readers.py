"""Every file digest consumer participates in the same reader protocol."""

import hashlib
import importlib

import pytest

from pixav.shared.media_file import open_media_file


@pytest.mark.parametrize(
    "module,function",
    [
        ("pixav.pixel_injector.canary", "sha256"),
        ("pixav.strm_resolver.real_av_acceptance", "file_digest"),
        ("pixav.strm_resolver.av_acceptance", "_artifact_sha256"),
        ("pixav.strm_resolver.movie_acceptance", "_local_digest"),
    ],
)
def test_acceptance_digest_refuses_a_live_writer_and_never_changes_atime(tmp_path, module, function):
    import os

    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"synthetic media")
    os.utime(path, ns=(1_000_000_000, path.stat().st_mtime_ns))
    read = getattr(importlib.import_module(module), function)
    before = path.stat().st_atime_ns
    with open_media_file(path, exclusive=True), pytest.raises(BlockingIOError):
        read(path)
    assert read(path) == hashlib.sha256(b"synthetic media").hexdigest()
    assert path.stat().st_atime_ns == before
