import json
from pathlib import Path

import pytest

from scripts.cleanup_watermark_garbage import (
    decode_xor_watermark,
    has_artifact,
    is_known_watermark,
    payload_matches,
    validate_database_backup,
    write_selected_backup,
)


def _encode_watermark(text: str, key: int = 0xA5) -> str:
    return bytes([key, *(byte ^ key for byte in text.encode("ascii"))]).hex()


def test_exact_known_watermark_is_detected() -> None:
    encoded = _encode_watermark("sehuatang@gmail.com")

    assert decode_xor_watermark(encoded) == "sehuatang@gmail.com"
    assert is_known_watermark(encoded)


def test_other_printable_obfuscation_is_not_destructively_classified() -> None:
    encoded = _encode_watermark("different@example.t")

    assert decode_xor_watermark(encoded) == "different@example.t"
    assert not is_known_watermark(encoded)


def test_real_info_hash_is_not_known_watermark() -> None:
    assert not is_known_watermark("0123456789abcdef0123456789abcdef01234567")


def test_payload_matches_task_or_video() -> None:
    raw = json.dumps({"task_id": "task-1", "video_id": "video-1"})

    assert payload_matches(raw, task_ids={"task-1"}, video_ids=set())
    assert payload_matches(raw.encode(), task_ids=set(), video_ids={"video-1"})
    assert not payload_matches("not-json", task_ids={"task-1"}, video_ids={"video-1"})


@pytest.mark.parametrize("column", ["local_path", "share_url", "cdn_url"])
def test_every_artifact_column_blocks_cleanup(column: str) -> None:
    assert has_artifact({column: "present"})


def test_artifact_check_tolerates_post_009_rows_without_cdn_url() -> None:
    assert not has_artifact({"local_path": None, "share_url": None})


def test_selected_backup_is_never_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDateTime:
        @classmethod
        def now(cls, _timezone):
            from datetime import datetime, timezone

            return datetime(2026, 8, 30, tzinfo=timezone.utc)

    monkeypatch.setattr("scripts.cleanup_watermark_garbage.datetime", FixedDateTime)
    first = write_selected_backup(backup_dir=tmp_path, videos=[], tasks=[], replay_audit=[])

    assert first.is_file()
    with pytest.raises(FileExistsError):
        write_selected_backup(backup_dir=tmp_path, videos=[], tasks=[], replay_audit=[])


def test_apply_backup_must_be_nonempty_regular_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.dump"
    empty.touch()

    with pytest.raises(RuntimeError):
        validate_database_backup(empty)
    with pytest.raises(OSError):
        validate_database_backup(tmp_path / "missing.dump")

    backup = tmp_path / "full.dump"
    backup.write_bytes(b"PGDMP")
    assert validate_database_backup(backup) == backup.resolve()
