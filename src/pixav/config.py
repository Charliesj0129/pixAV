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
    system_pause_key: str = "system:pause"

    # qBittorrent
    qbit_url: str = "http://localhost:8085"
    qbit_user: str = "admin"
    qbit_password: str = "adminadmin"

    # Media-Loader
    # Modes:
    # - full: real download + remux
    # - verify: qBit connectivity + create placeholder local file + route-to-upload
    media_loader_mode: str = "full"

    # Optional embeddings (very heavy deps: sentence-transformers/torch).
    # Keep disabled for MVP; enable only when you install the `embeddings` dependency group.
    embeddings_enabled: bool = False

    # Redroid
    redroid_image: str = "redroid/redroid:14.0.0-latest"
    redroid_network: str = "bridge"
    redroid_adb_host: str = "127.0.0.1"
    redroid_adb_port_start: int = 5555

    # Pixel-Injector
    # Modes:
    # - redroid: spawn Redroid + ADB + UI automation (Google Photos)
    # - local: mark upload complete and set a local share_url scheme for local resolver
    pixel_injector_mode: str = "redroid"
    pixel_injector_local_share_scheme: str = "pixav-local://"

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
    # Format: "URL|tag1,tag2;URL2|tag3"
    crawl_seed_urls: str = ""
    # Filter which internal links to visit from seed pages. Leave blank to visit everything.
    # Common forum patterns include either "thread-..." or "viewthread" style routes.
    crawl_link_filter_pattern: str = r"(viewthread|thread)"
    crawl_queries: str = ""
    crawl_max_pages: int = 50
    crawl_interval_seconds: int = 3600
    # Optional cookies for crawling (raw Cookie header or Netscape cookie file path).
    crawl_cookie_header: str = ""
    crawl_cookie_file: str = ""

    # Redis queue names
    queue_crawl: str = "pixav:crawl"
    queue_download: str = "pixav:download"
    queue_download_dlq: str = "pixav:download:dlq"
    queue_upload: str = "pixav:upload"
    queue_upload_dlq: str = "pixav:upload:dlq"
    queue_verify: str = "pixav:verify"

    # Retry
    download_max_retries: int = 10
    upload_max_retries: int = 10
    upload_dlq_replay_max: int = 3
    upload_dlq_replay_backoff_seconds: str = "60,300,900"

    # Upload execution controls
    upload_max_concurrency: int = 1
    upload_lock_key: str = "pixav:upload:lock"
    upload_lock_ttl_seconds: int = 7200
    upload_task_timeout_seconds: int = 3600
    upload_ready_timeout_seconds: int = 120
    upload_verify_timeout_seconds: int = 300

    # Scheduling policy
    no_account_policy: str = "wait"

    # Rate limiting
    resolver_rate_limit_rpm: int = 60
    resolver_concurrency: int = 3

    # STRM
    strm_output_dir: str = "./data/strm"
    resolver_base_url: str = "http://localhost:8000"

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"


def get_settings() -> Settings:
    """Factory — allows overriding in tests."""
    return Settings()
