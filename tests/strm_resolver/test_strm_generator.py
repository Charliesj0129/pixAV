"""Tests for STRM generator."""

from __future__ import annotations

from pathlib import Path

import pytest

from pixav.strm_resolver.strm_generator import _sanitize_filename, generate_strm


@pytest.mark.asyncio
async def test_generate_strm(tmp_path: Path) -> None:
    video_id = "550e8400-e29b-41d4-a716-446655440000"
    code = "ABC-123"
    title = "Test Video Title"
    base_url = "http://localhost:8000"
    output_dir = str(tmp_path / "strm")

    path = await generate_strm(video_id, code, title, base_url, output_dir)

    expected_path = tmp_path / "strm" / "ABC-123 - Test Video Title.strm"
    assert path == str(expected_path)
    assert Path(path).exists()
    assert Path(path).read_text(encoding="utf-8") == f"http://localhost:8000/stream/{video_id}"


def test_sanitize_filename() -> None:
    assert _sanitize_filename("ABC/123") == "ABC123"
    assert _sanitize_filename("Title: Subtitle") == "Title Subtitle"
    assert _sanitize_filename("../hack") == "hack"
