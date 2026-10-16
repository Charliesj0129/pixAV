"""Container roles load mounted secrets without exposing them in settings."""

from urllib.parse import unquote, urlsplit

import pytest

from pixav.config import Settings, get_settings


def test_settings_factory_reads_mounted_secret_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PIXAV_DB_PASSWORD", raising=False)
    monkeypatch.delenv("PIXAV_REDIS_URL", raising=False)
    (tmp_path / "PIXAV_DB_PASSWORD").write_text("synthetic-file-password\n")
    (tmp_path / "PIXAV_REDIS_URL").write_text("redis://:synthetic-file-password@redis:6379/0\n")
    monkeypatch.setenv("PIXAV_SETTINGS_SECRETS_DIR", str(tmp_path))
    settings = get_settings()
    assert settings.db_password == "synthetic-file-password"
    assert settings.redis_url == "redis://:synthetic-file-password@redis:6379/0"


def test_explicit_missing_secret_directory_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXAV_SETTINGS_SECRETS_DIR", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="SETTINGS_SECRET_DIRECTORY_UNAVAILABLE"):
        get_settings()


def test_settings_repr_does_not_contain_credentials():
    settings = Settings(
        db_password="synthetic-db-secret",
        redis_url="redis://:synthetic-redis-secret@redis:6379/0",
        qbit_password="synthetic-qbit-secret",
        jackett_api_key="synthetic-api-secret",
    )
    rendered = repr(settings)
    assert all(
        value not in rendered
        for value in ["synthetic-db-secret", "synthetic-redis-secret", "synthetic-qbit-secret", "synthetic-api-secret"]
    )


def test_dsn_round_trip_preserves_reserved_characters():
    settings = Settings(db_user="fixture@role", db_password="synthetic:/?#@%", db_name="synthetic/database")
    parsed = urlsplit(settings.dsn)
    assert unquote(parsed.username) == settings.db_user
    assert unquote(parsed.password) == settings.db_password
    assert unquote(parsed.path.removeprefix("/")) == settings.db_name
    assert parsed.hostname == settings.db_host
