"""Versioned Jellyfin files; one atomic directory pointer exposes a whole item."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from pixav.strm_resolver.playback import safe_file

FIELDS = ("title", "description", "release", "studio", "tags", "performers")


def effective_metadata(title: str, metadata: dict) -> dict:
    """Keep provider facts separate; explicit manual overrides always win."""
    effective = {"title": title}
    effective.update({key: metadata[key] for key in FIELDS if key in metadata})
    for name in sorted(metadata.get("providers", {})):
        provider = metadata["providers"][name]
        effective.update({key: provider[key] for key in FIELDS if key in provider})
    manual = metadata.get("manual_overrides", {})
    effective.update({key: manual[key] for key in FIELDS if key in manual})
    return effective


def nfo(metadata: dict) -> bytes:
    movie = ET.Element("movie")
    for source, target in (("title", "title"), ("description", "plot"), ("release", "premiered"), ("studio", "studio")):
        if metadata.get(source):
            ET.SubElement(movie, target).text = str(metadata[source])
    for tag in metadata.get("tags", []) or []:
        ET.SubElement(movie, "tag").text = str(tag)
    for person in metadata.get("performers", []) or []:
        ET.SubElement(ET.SubElement(movie, "actor"), "name").text = str(person)
    return ET.tostring(movie, encoding="utf-8", xml_declaration=True)


def stable_url(base_url: str, video_id: uuid.UUID) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("public playback URL must be an HTTP origin without credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("public playback URL cannot carry tokens")
    return f"{base_url.rstrip('/')}/stream/{video_id}"


class LibraryProjection:
    def __init__(self, pool, *, root: Path, artwork_root: Path, base_url: str):
        self.pool = pool
        self.root = root.absolute()
        self.artwork_root = artwork_root.absolute()
        self.base_url = base_url

    def _directories(self):
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise ValueError("unsafe projection root")
        for name in ("items", "versions"):
            target = self.root / name
            if target.is_symlink():
                raise ValueError("unsafe projection directory")
            target.mkdir(parents=True, exist_ok=True)

    def _activate(self, video_id, version: Path) -> Path:
        active = self.root / "items" / str(video_id)
        if active.exists() and not active.is_symlink():
            raise ValueError("refusing to replace an unowned library directory")
        temporary = active.with_name(f".{video_id}-{uuid.uuid4().hex}")
        try:
            temporary.symlink_to(os.path.relpath(version, temporary.parent), target_is_directory=True)
            os.replace(temporary, active)
        finally:
            temporary.unlink(missing_ok=True)
        return active

    def _poster(self, poster: Path) -> tuple[bytes, str, str]:
        from PIL import Image

        if not safe_file(self.artwork_root, poster):
            raise ValueError("retained artwork required")
        content = poster.read_bytes()
        with Image.open(BytesIO(content)) as image:
            suffix = {"JPEG": "jpg", "PNG": "png"}.get(image.format or "")
            image.verify()
        if suffix is None:
            raise ValueError("poster must be JPEG or PNG")
        return content, suffix, hashlib.sha256(content).hexdigest()

    def _version(self, video_id, revision: str, files: dict[str, bytes]) -> Path:
        version = self.root / "versions" / f"{video_id}-{revision}"
        if version.is_symlink():
            raise ValueError("unsafe projection version")
        version.mkdir(exist_ok=True)
        for name, content in files.items():
            target = version / name
            if target.is_symlink():
                raise ValueError("unsafe projection file")
            if target.exists():
                if target.read_bytes() != content:
                    raise ValueError("published revision content mismatch")
            else:
                target.write_bytes(content)
        return version

    async def _write(self, conn, video_id, poster):
        video = await conn.fetchrow("SELECT * FROM videos WHERE id=$1 FOR UPDATE", video_id)
        playable = await conn.fetchrow(
            """SELECT p.* FROM playable_assets p JOIN remote_assets r ON r.id=p.remote_asset_id
            WHERE p.video_id=$1 AND p.state='READY' AND r.state='DURABLE' FOR SHARE OF p,r""",
            video_id,
        )
        if video is None or playable is None:
            raise ValueError("only durable, playback-ready media can be published")
        previous = await conn.fetchrow("SELECT * FROM library_publications WHERE video_id=$1", video_id)
        if poster is None and previous and previous["poster_path"]:
            poster = Path(previous["poster_path"])
        if poster is None:
            raise ValueError("retained artwork required")
        content, suffix, poster_hash = self._poster(poster)
        raw = video["metadata_json"] or {}
        metadata = effective_metadata(video["title"], json.loads(raw) if isinstance(raw, str) else dict(raw))
        stream = stable_url(self.base_url, video_id)
        revision = hashlib.sha256(
            json.dumps([metadata, poster_hash, stream, playable["manifest_sha256"]], sort_keys=True).encode()
        ).hexdigest()
        version = self._version(
            video_id,
            revision,
            {
                "movie.strm": stream.encode(),
                "movie.nfo": nfo(metadata),
                f"poster.{suffix}": content,
            },
        )
        await conn.execute(
            """INSERT INTO library_publications(video_id,state,manifest_sha256,revision,metadata,poster_path,poster_sha256)
            VALUES($1,'PUBLISHED',$2,$3,$4::jsonb,$5,$6) ON CONFLICT(video_id) DO UPDATE SET
            state='PUBLISHED',manifest_sha256=$2,revision=$3,metadata=$4::jsonb,poster_path=$5,
            poster_sha256=$6,updated_at=now()""",
            video_id,
            playable["manifest_sha256"],
            revision,
            json.dumps(metadata),
            str(poster.absolute()),
            poster_hash,
        )
        return version

    async def publish(self, video_id: uuid.UUID, poster: Path | None = None) -> Path:
        self._directories()
        active = self.root / "items" / str(video_id)
        old_target = None
        activated = False
        try:
            async with self.pool.acquire() as conn, conn.transaction():
                version = await self._write(conn, video_id, poster)
                old_target = active.readlink() if active.is_symlink() else None
                self._activate(video_id, version)
                activated = True
        except BaseException:
            # Includes COMMIT failure. A process crash is reconciled by rerunning
            # publish from PostgreSQL; the visible pointer always names whole files.
            if activated:
                if old_target is None:
                    active.unlink(missing_ok=True)
                else:
                    self._activate(video_id, active.parent / old_target)
            raise
        return active

    async def withdraw_invalid(self, video_id: uuid.UUID | None = None) -> int:
        """Remove only owned active pointers, never remote media or retained art."""
        self._directories()
        count = 0
        rows = await self.pool.fetch(
            "SELECT video_id FROM library_publications WHERE ($1::uuid IS NULL OR video_id=$1)", video_id
        )
        for row in rows:
            video_id = row["video_id"]
            async with self.pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT id FROM videos WHERE id=$1 FOR UPDATE", video_id)
                valid = await conn.fetchval(
                    """SELECT EXISTS(SELECT FROM playable_assets p JOIN remote_assets r ON r.id=p.remote_asset_id
                    WHERE p.video_id=$1 AND p.state='READY' AND r.state='DURABLE')""",
                    video_id,
                )
                if valid:
                    continue
                active = self.root / "items" / str(video_id)
                if active.is_symlink() and active.resolve().parent == (self.root / "versions").resolve():
                    active.unlink()
                    count += 1
                await conn.execute(
                    "UPDATE library_publications SET state='STALE',updated_at=now() WHERE video_id=$1", video_id
                )
        return count
