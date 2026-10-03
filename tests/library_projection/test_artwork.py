"""Retained artwork originals (BDD-098, 100)."""

from __future__ import annotations

import hashlib

import pytest

from pixav.library_projection.artwork import ArtworkIntegrityError, ArtworkStore, sniff_media_type

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic png body"
JPEG = b"\xff\xd8\xff\xe0" + b"synthetic jpeg body"
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"synthetic"


@pytest.mark.parametrize(("content", "media_type"), [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")])
def test_images_are_recognised_by_signature(content, media_type):
    assert sniff_media_type(content) == media_type


def test_import_retains_the_original_under_its_digest(tmp_path):
    source = tmp_path / "poster.png"
    source.write_bytes(PNG)
    store = ArtworkStore(tmp_path / "artwork")

    digest, size, media_type = store.import_file(source)

    assert digest == hashlib.sha256(PNG).hexdigest() and size == len(PNG) and media_type == "image/png"
    assert store.path_for(digest).read_bytes() == PNG
    assert [p.name for p in store.path_for(digest).parent.iterdir()] == [digest], "no temporary file is left"


def test_importing_the_same_bytes_twice_is_one_original(tmp_path):
    source = tmp_path / "poster.jpg"
    source.write_bytes(JPEG)
    store = ArtworkStore(tmp_path / "artwork")

    assert store.import_file(source) == store.import_file(source)


@pytest.mark.parametrize("content", [b"GIF89a not accepted", b"", b"<svg/>"])
def test_only_jpeg_png_and_webp_are_accepted(tmp_path, content):
    source = tmp_path / "poster.bin"
    source.write_bytes(content)

    with pytest.raises(ValueError, match="JPEG, PNG or WebP"):
        ArtworkStore(tmp_path / "artwork").import_file(source)


def test_a_link_is_not_imported(tmp_path):
    real = tmp_path / "real.png"
    real.write_bytes(PNG)
    link = tmp_path / "link.png"
    link.symlink_to(real)

    with pytest.raises(OSError):
        ArtworkStore(tmp_path / "artwork").import_file(link)


def test_oversized_artwork_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("pixav.library_projection.artwork.MAX_ARTWORK_BYTES", 16)
    source = tmp_path / "poster.png"
    source.write_bytes(PNG)

    with pytest.raises(ValueError, match="larger"):
        ArtworkStore(tmp_path / "artwork").import_file(source)


def test_retained_original_is_read_back_only_if_unchanged_bdd_100(tmp_path):
    source = tmp_path / "poster.png"
    source.write_bytes(PNG)
    store = ArtworkStore(tmp_path / "artwork")
    digest, size, _ = store.import_file(source)
    source.unlink()  # the provider or the operator's copy is gone; the retained original is not

    assert store.read_verified(digest, size) == PNG

    store.path_for(digest).write_bytes(PNG[:-1] + b"X")
    with pytest.raises(ArtworkIntegrityError):
        store.read_verified(digest, size)


def test_a_missing_original_is_an_integrity_failure(tmp_path):
    with pytest.raises(ArtworkIntegrityError):
        ArtworkStore(tmp_path).read_verified("a" * 64, 10)


def test_digest_names_are_never_paths(tmp_path):
    with pytest.raises(ValueError):
        ArtworkStore(tmp_path).path_for("../" + "a" * 61)
