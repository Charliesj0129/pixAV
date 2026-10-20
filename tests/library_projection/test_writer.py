"""Atomic projection publication on a real filesystem (BDD-101-104, 106, 107)."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest

from pixav.library_projection.artwork import ArtworkIntegrityError, ArtworkStore
from pixav.library_projection.metadata import EffectiveMetadata
from pixav.library_projection.render import render_projection
from pixav.library_projection.repository import ArtworkRecord
from pixav.library_projection.writer import ForeignContentError, ProjectionWriter, publication_is_complete
from pixav.shared.media_file import open_media_file

VIDEO = uuid.UUID("99999999-9999-4999-8999-999999999999")
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic poster"


@pytest.fixture
def store(tmp_path) -> ArtworkStore:
    return ArtworkStore(tmp_path / "artwork")


@pytest.fixture
def writer(tmp_path, store) -> ProjectionWriter:
    return ProjectionWriter(tmp_path / "root", store)


def retained_poster(tmp_path, store) -> ArtworkRecord:
    source = tmp_path / "poster.png"
    source.write_bytes(PNG)
    digest, size, media_type = store.import_file(source)
    return ArtworkRecord(
        id=uuid.uuid4(), video_id=VIDEO, kind="poster", sha256=digest, size_bytes=size, media_type=media_type
    )


def spec(title: str = "Synthetic", artwork=()):
    return render_projection(VIDEO, EffectiveMetadata(title=title), list(artwork), resolver_base_url="http://r:8000")


def item(writer) -> str:
    return str(writer.library / f"pixav-{VIDEO}")


def filesystem_metadata(paths):
    result = []
    for path in paths:
        info = path.lstat()
        result.append((*info, info.st_atime_ns, info.st_mtime_ns, info.st_ctime_ns))
    return result


def test_publish_activates_a_complete_verified_version(tmp_path, store, writer):
    first = spec(artwork=[retained_poster(tmp_path, store)])

    version = writer.publish(first, 1)

    assert version == f"1-{first.digest[:12]}"
    assert os.readlink(item(writer)) == f"../versions/{VIDEO}/{version}"
    assert sorted(os.listdir(item(writer))) == [f"pixav-{VIDEO}.nfo", f"pixav-{VIDEO}.strm", "poster.png"]
    assert (writer.library / f"pixav-{VIDEO}" / "poster.png").read_bytes() == PNG
    assert writer.matches(VIDEO, version, first)


def test_republishing_the_same_spec_is_idempotent_bdd_089(writer):
    first = spec()

    assert writer.publish(first, 1) == writer.publish(first, 1)
    assert os.listdir(writer.library) == [f"pixav-{VIDEO}"]
    assert os.listdir(writer.versions / str(VIDEO)) == [f"1-{first.digest[:12]}"]


def test_new_version_replaces_the_old_one_atomically_bdd_102(writer):
    old = writer.publish(spec("Old"), 1)
    reader = open(os.path.join(item(writer), f"pixav-{VIDEO}.nfo"), "rb")  # noqa: SIM115 - held across the swap

    new = writer.publish(spec("New"), 2)

    assert b"Old" in reader.read()
    reader.close()
    assert b"New" in (writer.library / f"pixav-{VIDEO}" / f"pixav-{VIDEO}.nfo").read_bytes()
    assert sorted(os.listdir(writer.versions / str(VIDEO))) == sorted([old, new]), "the previous version is kept"

    third = writer.publish(spec("Third"), 3)
    assert sorted(os.listdir(writer.versions / str(VIDEO))) == sorted([new, third]), "older ones are pruned"


def test_a_failure_before_every_file_is_ready_never_activates_bdd_101(tmp_path, store, writer):
    active = writer.publish(spec("Good"), 1)
    record = retained_poster(tmp_path, store)
    store.path_for(record.sha256).write_bytes(b"damaged")

    with pytest.raises(ArtworkIntegrityError):
        writer.publish(spec("Better", artwork=[record]), 2)

    assert writer.active_version(VIDEO) == active
    assert b"Good" in (writer.library / f"pixav-{VIDEO}" / f"pixav-{VIDEO}.nfo").read_bytes()
    assert os.listdir(writer.versions / str(VIDEO)) == [active], "no partial directory is left behind"


def test_deleted_files_are_noticed_bdd_104_106(writer):
    published = spec()
    version = writer.publish(published, 1)
    (writer.versions / str(VIDEO) / version / f"pixav-{VIDEO}.nfo").unlink()

    assert not writer.matches(VIDEO, version, published)

    (writer.library / f"pixav-{VIDEO}").unlink()
    assert writer.active_version(VIDEO) is None


def test_foreign_content_at_an_item_name_is_refused_not_removed(writer):
    writer.library.mkdir(parents=True)
    foreign = writer.library / f"pixav-{VIDEO}"
    foreign.mkdir()
    (foreign / "operator.txt").write_text("keep me")

    with pytest.raises(ForeignContentError):
        writer.publish(spec(), 1)
    with pytest.raises(ForeignContentError):
        writer.withdraw(VIDEO)
    assert (foreign / "operator.txt").read_text() == "keep me"


def test_a_link_elsewhere_is_foreign(writer, tmp_path):
    writer.library.mkdir(parents=True)
    (writer.library / f"pixav-{VIDEO}").symlink_to(tmp_path)

    with pytest.raises(ForeignContentError):
        writer.active_version(VIDEO)


def test_withdraw_removes_only_what_it_created_bdd_107(writer):
    writer.publish(spec(), 1)
    other = writer.versions / str(VIDEO) / "notes.txt"
    other.write_text("not ours")

    assert writer.withdraw(VIDEO) is True

    assert not os.path.lexists(item(writer))
    assert other.read_text() == "not ours"
    assert writer.withdraw(uuid.uuid4()) is False


def test_items_lists_published_and_leftover_videos(writer):
    writer.publish(spec(), 1)
    stray = uuid.uuid4()
    (writer.versions / str(stray)).mkdir()
    (writer.versions / "not-a-video").mkdir()

    assert writer.items() == {VIDEO, stray}


def test_crash_leftovers_are_cleared_on_the_next_publish(writer):
    writer.library.mkdir(parents=True)
    (writer.library / f".pixav-{VIDEO}.link-0123456789abcdef").symlink_to("../versions/x")
    root = writer.versions / str(VIDEO)
    root.mkdir(parents=True)
    (root / ".1-aaaaaaaaaaaa.partial-0123456789abcdef").mkdir()

    version = writer.publish(spec(), 2)

    assert os.listdir(writer.library) == [f"pixav-{VIDEO}"]
    assert os.listdir(root) == [version]


@pytest.mark.parametrize("damage", ["same_size", "missing", "symlink", "extra"])
def test_cleanup_checks_full_committed_publication(tmp_path, store, writer, damage):
    projection = spec(artwork=[retained_poster(tmp_path, store)])
    version = writer.publish(projection, 1)
    root = writer.library.parent
    assert publication_is_complete(root, VIDEO, version, projection.digest)
    poster = writer.versions / str(VIDEO) / version / "poster.png"
    if damage == "same_size":
        poster.write_bytes(b"x" * len(PNG))
    elif damage == "missing":
        poster.unlink()
    elif damage == "symlink":
        poster.unlink()
        poster.symlink_to(store.path_for(projection.files[-1].sha256))
    else:
        (poster.parent / "extra.txt").write_text("unexpected")
    assert not publication_is_complete(root, VIDEO, version, projection.digest)


def test_cleanup_rejects_posterless_projection_even_with_matching_digest(writer):
    projection = spec()
    version = writer.publish(projection, 1)
    assert not publication_is_complete(writer.library.parent, VIDEO, version, projection.digest)


def test_cleanup_publication_verification_does_not_change_file_metadata(tmp_path, store, writer):
    projection = spec(artwork=[retained_poster(tmp_path, store)])
    version = writer.publish(projection, 1)
    paths = list((writer.versions / str(VIDEO) / version).iterdir())
    for path in paths:
        os.utime(path, ns=(1_000_000_000, path.stat().st_mtime_ns))

    def metadata():
        return [
            (p.stat().st_ino, p.stat().st_size, p.stat().st_atime_ns, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
            for p in paths
        ]

    before = metadata()
    assert publication_is_complete(writer.library.parent, VIDEO, version, projection.digest)
    assert metadata() == before


def test_cleanup_publication_verification_refuses_an_active_writer(tmp_path, store, writer):
    projection = spec(artwork=[retained_poster(tmp_path, store)])
    version = writer.publish(projection, 1)
    poster = writer.versions / str(VIDEO) / version / "poster.png"
    with open_media_file(poster, exclusive=True):
        assert not publication_is_complete(writer.library.parent, VIDEO, version, projection.digest)


def test_cleanup_publication_verification_preserves_symlink_and_directory_metadata(tmp_path, store, writer):
    projection = spec(artwork=[retained_poster(tmp_path, store)])
    version = writer.publish(projection, 1)
    directory = writer.versions / str(VIDEO) / version
    paths = [
        writer.library.parent,
        writer.library,
        writer.library / f"pixav-{VIDEO}",
        writer.versions,
        directory.parent,
        directory,
        *directory.iterdir(),
    ]
    for path in paths:
        os.utime(path, ns=(1_000_000_000, path.lstat().st_mtime_ns), follow_symlinks=False)
    before = filesystem_metadata(paths)
    flags = os.statvfs(writer.library.parent).f_flag

    assert publication_is_complete(writer.library.parent, VIDEO, version, projection.digest)

    assert filesystem_metadata(paths) == before
    assert os.statvfs(writer.library.parent).f_flag == flags


def test_cleanup_rejects_a_projection_parent_symlink_without_accessing_its_target(tmp_path, store, writer):
    projection = spec(artwork=[retained_poster(tmp_path, store)])
    version = writer.publish(projection, 1)
    outside = tmp_path / "outside-versions"
    writer.versions.rename(outside)
    writer.versions.symlink_to(outside, target_is_directory=True)
    paths = [writer.versions, outside, outside / str(VIDEO), writer.library / f"pixav-{VIDEO}"]
    for path in paths:
        os.utime(path, ns=(1_000_000_000, path.lstat().st_mtime_ns), follow_symlinks=False)
    before = filesystem_metadata(paths)

    assert not publication_is_complete(writer.library.parent, VIDEO, version, projection.digest)

    assert filesystem_metadata(paths) == before


def test_cleanup_refuses_when_readonly_namespace_is_unavailable_without_touching_the_link(
    tmp_path, store, writer, monkeypatch
):
    from pixav.library_projection import readonly_cleanup

    projection = spec(artwork=[retained_poster(tmp_path, store)])
    version = writer.publish(projection, 1)
    link = writer.library / f"pixav-{VIDEO}"
    os.utime(link, ns=(1_000_000_000, link.lstat().st_mtime_ns), follow_symlinks=False)
    before = filesystem_metadata([link, writer.library])
    monkeypatch.setattr(readonly_cleanup.shutil, "which", lambda _: None)

    assert not publication_is_complete(writer.library.parent, VIDEO, version, projection.digest)

    assert filesystem_metadata([link, writer.library]) == before


def test_cleanup_refuses_a_real_nested_mount_instead_of_checking_hidden_underlying_files(tmp_path, store, writer):
    projection = spec(artwork=[retained_poster(tmp_path, store)])
    version = writer.publish(projection, 1)
    mounted = tmp_path / "different-versions"
    mounted.mkdir()
    payload = r"""
import subprocess,sys
from pathlib import Path
from uuid import UUID
from pixav.library_projection.readonly_cleanup import _has_nested_mount
from pixav.library_projection.writer import publication_is_complete
root,mounted,video,version,digest=sys.argv[1:]
subprocess.run(['/usr/bin/mount','--bind',mounted,str(Path(root)/'versions')],check=True,capture_output=True)
assert _has_nested_mount(Path(root))
assert not publication_is_complete(Path(root),UUID(video),version,digest)
print('NESTED_MOUNT_REFUSED')
"""
    result = subprocess.run(  # noqa: S603 -- actual private namespace and synthetic projection only
        [
            "/usr/bin/unshare",
            "--user",
            "--map-root-user",
            "--mount",
            "--propagation=private",
            "--",
            sys.executable,
            "-c",
            payload,
            str(writer.library.parent),
            str(mounted),
            str(VIDEO),
            version,
            projection.digest,
        ],
        env={
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "PYTHONPATH": os.pathsep.join(sys.path),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        timeout=20,
        check=True,
    )
    assert result.stdout == b"NESTED_MOUNT_REFUSED\n" and not result.stderr
    assert (writer.versions / str(VIDEO) / version / "poster.png").is_file()
