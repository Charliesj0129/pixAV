"""Interfaces for Media-Loader module."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TorrentClient(Protocol):
    """Protocol for torrent client implementations."""

    async def add_magnet(self, uri: str) -> str:
        """Add a magnet URI to the torrent client.

        Args:
            uri: Magnet URI to add.

        Returns:
            Torrent hash identifier.
        """
        ...

    async def wait_complete(self, torrent_hash: str, timeout: int = 3600) -> str:
        """Wait for a torrent to complete downloading.

        Args:
            torrent_hash: Hash of the torrent to wait for.
            timeout: Maximum time to wait in seconds (default: 3600).

        Returns:
            Path to the downloaded content.
        """

    async def delete_torrent(self, torrent_hash: str, delete_files: bool = True) -> None:
        """Delete a torrent and optionally its files.

        Args:
            torrent_hash: Hash of the torrent to delete.
            delete_files: Whether to delete downloaded files (default: True).
        """
        ...


@runtime_checkable
class Remuxer(Protocol):
    """Protocol for media remuxing."""

    async def remux(self, input_path: str, output_path: str) -> None:
        """Remux media from input to output format.

        Args:
            input_path: Path to input media file.
            output_path: Path to write remuxed output.
        """
        ...


@runtime_checkable
class MetadataScraper(Protocol):
    """Protocol for metadata scraping."""

    async def scrape(self, title: str) -> dict[str, Any]:
        """Scrape metadata for a media title.

        Args:
            title: Title of the media to scrape metadata for.

        Returns:
            Dictionary containing metadata.
        """
        ...
