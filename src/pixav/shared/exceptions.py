"""Hierarchical exception types for the pixAV pipeline."""

from __future__ import annotations


class PixavError(Exception):
    """Base exception for all pixAV errors."""


# ── Infrastructure ──────────────────────────────────────────────


class DatabaseError(PixavError):
    """Failed to communicate with PostgreSQL."""


class RedisError(PixavError):
    """Failed to communicate with Redis."""


class QueueError(RedisError):
    """Queue-level operation failed."""


# ── Pixel-Injector ──────────────────────────────────────────────


class RedroidError(PixavError):
    """Redroid container lifecycle error."""


class AdbError(PixavError):
    """ADB connection or command error."""


class UploadError(PixavError):
    """Google Photos upload failed."""


class VerificationError(PixavError):
    """Share URL verification failed."""


# ── Media-Loader ────────────────────────────────────────────────


class DownloadError(PixavError):
    """Torrent download failed."""


class SourceUnavailableError(DownloadError):
    """This source candidate cannot deliver the media.

    Distinct from a plain :class:`DownloadError`, which means the torrent client
    itself misbehaved and is worth retrying. A source is unavailable when the
    swarm is dead or the magnet was never a torrent at all, so the correct
    response is to cool the candidate down and try another source, not to walk
    the retry backoff against the same dead swarm.
    """


class RemuxError(PixavError):
    """FFmpeg remux failed."""


# ── SHT-Probe ──────────────────────────────────────────────────


class CrawlError(PixavError):
    """Crawl or parse failed."""


# ── Strm-Resolver ──────────────────────────────────────────────


class ResolveError(PixavError):
    """CDN URL resolution failed."""


class MediaDependencyError(RemuxError):
    """Media inspection or preparation dependency is unavailable."""


class TorrentOwnershipError(DownloadError):
    """External torrent ownership or add result requires manual reconciliation."""
