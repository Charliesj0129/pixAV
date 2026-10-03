"""Level-triggered library reconciliation (BDD-085, 086, 089, 103-108).

Each pass compares what PostgreSQL says should be published with what the
library shows, and converges one toward the other:

* a video whose READY object is on a DURABLE copy is published, or re-published
  when its projection is missing, damaged or out of date;
* a projection whose files are already right but whose row does not say so
  (a crash between the link swap and the commit) is recorded, not rewritten;
* anything published for a video that is no longer playable is withdrawn.

Withdrawal removes only the projection. No remote asset, segment or provider
is touched, so the durable copy stays exactly as it was (BDD-108).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pixav.config import Settings
from pixav.library_projection.artwork import ArtworkIntegrityError, ArtworkStore
from pixav.library_projection.metadata import EffectiveMetadata, effective_metadata, provider_metadata
from pixav.library_projection.render import LAYOUT_VERSION, ProjectionRefusedError, ProjectionSpec, render_projection
from pixav.library_projection.repository import ArtworkRecord, LibraryRepository, ProjectionSource, Publication
from pixav.library_projection.writer import ForeignContentError, ProjectionWriter
from pixav.shared.metrics import record_library_projection_failure, set_library_publications

logger = logging.getLogger(__name__)

WITHDRAWN_NOT_PLAYABLE = "NOT_PLAYABLE"
Action = Literal["unchanged", "publish", "record", "withdraw", "refuse"]


@dataclass(frozen=True)
class Prepared:
    """What one eligible video should look like in the library."""

    source: ProjectionSource
    metadata: EffectiveMetadata
    spec: ProjectionSpec


@dataclass(frozen=True)
class Decision:
    video_id: uuid.UUID
    action: Action
    row: Publication | None = None
    code: str | None = None
    prepared: Prepared | None = None
    active_version: str | None = None


class LibraryReconciler:
    def __init__(
        self,
        repository: LibraryRepository,
        writer: ProjectionWriter,
        *,
        resolver_base_url: str,
        write_limit: int = 50,
    ) -> None:
        self.repository = repository
        self.writer = writer
        self.resolver_base_url = resolver_base_url
        # A policy change republishes everything; spreading it over passes keeps
        # one tick from rewriting the whole library at once.
        self.write_limit = write_limit

    async def plan(self) -> list[Decision]:
        """Decide every change without writing anything."""
        sources = await self.repository.eligible_sources()
        publications = await self.repository.publications()
        on_disk = await asyncio.to_thread(self.writer.items)
        ids = [source.video_id for source in sources]
        overrides = await self.repository.overrides_for(ids)
        artwork = await self.repository.artwork_for(ids)
        decisions = [
            await self._decide(
                source,
                publications.get(source.video_id),
                overrides.get(source.video_id, {}),
                artwork.get(source.video_id, []),
            )
            for source in sources
        ]
        shown = {video for video, row in publications.items() if row.state != "PENDING" or row.active_digest}
        for video_id in sorted((shown | on_disk) - set(ids)):
            decisions.append(Decision(video_id, "withdraw", publications.get(video_id), WITHDRAWN_NOT_PLAYABLE))
        return decisions

    async def _decide(
        self,
        source: ProjectionSource,
        row: Publication | None,
        overrides: dict[str, object],
        artwork: list[ArtworkRecord],
    ) -> Decision:
        try:
            metadata = effective_metadata(
                video_title=source.title,
                video_tags=source.tags,
                providers=provider_metadata(source.metadata_json),
                overrides=overrides,
            )
            spec = render_projection(source.video_id, metadata, artwork, resolver_base_url=self.resolver_base_url)
            if not any(record.kind == "poster" for record in artwork):
                raise ProjectionRefusedError("POSTER_REQUIRED")
        except ProjectionRefusedError as exc:
            return Decision(source.video_id, "refuse", row, exc.code)
        try:
            active = await asyncio.to_thread(self._current, spec)
        except ForeignContentError:
            return Decision(source.video_id, "refuse", row, "PROJECTION_PATH_CONFLICT")
        prepared = Prepared(source, metadata, spec)
        if active is None:
            return Decision(source.video_id, "publish", row, prepared=prepared)
        committed = (
            row is not None
            and row.state == "PUBLISHED"
            and row.active_version == active
            and row.active_digest == spec.digest
            and row.playable_asset_id == source.playable_asset_id
        )
        return Decision(
            source.video_id, "unchanged" if committed else "record", row, prepared=prepared, active_version=active
        )

    def _current(self, spec: ProjectionSpec) -> str | None:
        """The active version when it already is this spec, judged by names and sizes."""
        active = self.writer.active_version(spec.video_id)
        if active is None or not self.writer.matches(spec.video_id, active, spec):
            return None
        return active

    async def reconcile(self) -> dict[str, int]:
        """One pass: apply the plan, bounded by the write limit, and export the states."""
        counts: Counter[str] = Counter()
        async with self.repository.exclusive() as held:
            if not held:
                return {"skipped": 1}
            writes = 0
            for decision in await self.plan():
                if decision.action == "publish" and writes >= self.write_limit:
                    counts["deferred"] += 1
                    continue
                writes += decision.action == "publish"
                counts[await self._apply(decision)] += 1
        set_library_publications(await self.repository.publication_counts())
        return dict(counts)

    async def _apply(self, decision: Decision) -> str:
        if decision.action == "unchanged":
            return "unchanged"
        if decision.action == "refuse":
            return await self._fail(decision.video_id, decision.code or "PROJECTION_REFUSED")
        if decision.action == "withdraw":
            return await self._withdraw(decision.video_id, decision.row)
        prepared = decision.prepared
        if prepared is None:
            raise RuntimeError("a publish decision carries its rendered projection")
        if decision.action == "record" and decision.active_version is not None:
            directory = self.writer.versions / str(decision.video_id) / decision.active_version
            # Recording trusts files it did not just write, so their bytes are read first.
            if await asyncio.to_thread(self.writer.verify, directory, prepared.spec):
                return await self._commit(prepared, decision.active_version, decision.row, written=False)
        return await self._publish(prepared, decision.row)

    async def _publish(self, prepared: Prepared, row: Publication | None) -> str:
        video_id, spec = prepared.source.video_id, prepared.spec
        await self.repository.begin_attempt(video_id, desired_digest=spec.digest, layout_version=LAYOUT_VERSION)
        try:
            version = await asyncio.to_thread(self.writer.publish, spec, (row.projection_version if row else 0) + 1)
        except ArtworkIntegrityError:
            return await self._fail(video_id, "ARTWORK_UNAVAILABLE")
        except ForeignContentError:
            return await self._fail(video_id, "PROJECTION_PATH_CONFLICT")
        except OSError as exc:
            logger.error("projection write failed for %s: %s", video_id, type(exc).__name__)
            return await self._fail(video_id, "PROJECTION_WRITE_FAILED")
        return await self._commit(prepared, version, row, written=True)

    async def _commit(self, prepared: Prepared, version: str, row: Publication | None, *, written: bool) -> str:
        source, spec = prepared.source, prepared.spec
        if row is None and not written:
            # Files from before a lost row; the row is recreated around them.
            await self.repository.begin_attempt(
                source.video_id, desired_digest=spec.digest, layout_version=LAYOUT_VERSION
            )
        committed = await self.repository.mark_published(
            source.video_id,
            playable_asset_id=source.playable_asset_id,
            digest=spec.digest,
            version=version,
            projection_version=int(version.split("-", 1)[0]),
            effective_metadata=prepared.metadata.model_dump(mode="json"),
        )
        if not committed:
            # The object stopped being playable meanwhile; the next pass withdraws it.
            return "superseded"
        return "published" if written else "recorded"

    async def _withdraw(self, video_id: uuid.UUID, row: Publication | None) -> str:
        try:
            await asyncio.to_thread(self.writer.withdraw, video_id)
        except ForeignContentError:
            if row is not None:
                return await self._fail(video_id, "PROJECTION_PATH_CONFLICT")
            record_library_projection_failure("PROJECTION_PATH_CONFLICT")
            return "failed"
        if row is not None:
            await self.repository.mark_withdrawn(video_id, WITHDRAWN_NOT_PLAYABLE)
        return "withdrawn"

    async def _fail(self, video_id: uuid.UUID, code: str) -> str:
        logger.warning("library projection for %s failed: %s", video_id, code)
        await self.repository.record_failure(video_id, code, layout_version=LAYOUT_VERSION)
        record_library_projection_failure(code)
        return "failed"


def build_reconciler(pool: Any, settings: Settings) -> LibraryReconciler:
    return LibraryReconciler(
        LibraryRepository(pool),
        ProjectionWriter(Path(settings.library_root), ArtworkStore(Path(settings.artwork_root))),
        resolver_base_url=settings.resolver_base_url,
    )
