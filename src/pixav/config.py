"""Centralised configuration via Pydantic Settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application-wide configuration loaded from environment variables."""

    model_config = {"env_prefix": "PIXAV_", "frozen": True}

    # PostgreSQL
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "pixav"
    db_password: str = "pixav"
    db_name: str = "pixav"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # qBittorrent
    qbit_url: str = "http://localhost:8080"
    qbit_user: str = "admin"
    qbit_password: str = "adminadmin"

    # Redroid
    redroid_image: str = "redroid/redroid:14.0.0-latest"
    redroid_adb_port_start: int = 5555

    # Strm-Resolver
    resolver_host: str = "0.0.0.0"
    resolver_port: int = 8000

    # Stash
    stash_url: str = "http://localhost:9999"

    # Downloads
    download_dir: str = "./data/downloads"

    # Jackett
    jackett_url: str = "http://localhost:9117"
    jackett_api_key: str = ""

    # FlareSolverr
    flaresolverr_url: str = "http://localhost:8191"

    # Crawl
    crawl_seed_urls: str = ""  # comma-separated list of seed URLs
    crawl_interval_seconds: int = 3600

    # Redis queue names
    queue_crawl: str = "pixav:crawl"
    queue_download: str = "pixav:download"
    queue_upload: str = "pixav:upload"
    queue_verify: str = "pixav:verify"

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"


def get_settings() -> Settings:
    """Factory — allows overriding in tests."""
    return Settings()
