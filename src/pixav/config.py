"""Centralised configuration via Pydantic Settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="PIXAV_", frozen=True, extra="ignore")

    # PostgreSQL
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "pixav"
    db_password: str = "pixav"
    db_name: str = "pixav"

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    system_pause_key: str = "system:pause"
    download_pause_key: str = "pixav:download:pause"

    # qBittorrent
    qbit_url: str = "http://localhost:8085"
    qbit_user: str = "admin"
    qbit_password: str = "adminadmin"
    # Path as seen by qBittorrent inside its own container namespace. This is
    # intentionally separate from ``download_dir`` below: qBittorrent mounts
    # the host directory at /downloads while pixAV workers see it under /app.
    qbit_download_dir: str = "/downloads"
    # Optional comma/newline-separated fallback trackers for bare magnets.
    qbit_extra_trackers: str = ""
    # Overall wall-clock budget for one torrent download.
    qbit_download_timeout_seconds: int = 3600
    # No-peer observation timeout; this is an infrastructure retry, not proof
    # that the source is unavailable.
    qbit_no_peer_grace_seconds: int = 300
    # How long a source candidate stays cooled down after its swarm failed to
    # deliver. A dead swarm today may be alive next week, so candidates are
    # parked, never deleted.
    source_candidate_cooldown_hours: int = 6

    # Media-Loader
    # Modes:
    # - full: real download + remux
    # - verify: connectivity diagnosis only; never produces an upload artifact
    media_loader_mode: str = "full"
    managed_media_workflow: bool = False
    source_min_quality_score: int = 0

    # Optional embeddings (very heavy deps: sentence-transformers/torch).
    # Keep disabled for MVP; enable only when you install the `embeddings` dependency group.
    embeddings_enabled: bool = False

    # Redroid
    redroid_image: str = "redroid/redroid:14.0.0-latest"
    redroid_network: str = "bridge"
    redroid_adb_host: str = "127.0.0.1"
    redroid_adb_port_start: int = 5555
    # Device identity lives in YAML, not in code. The named profile supplies the
    # image (pin by digest in production) and the ro.* overrides applied to the
    # container, and it also supplies the readiness evidence that proves the
    # identity took effect.
    redroid_profile: str = "gphotos_pixel_xl_v1"
    redroid_profiles_path: str = "config/android_profiles.yml"

    # Pixel-Injector
    # Modes:
    # - redroid: spawn Redroid + ADB + UI automation (Google Photos)
    # - local: mark upload complete and set a local share_url scheme for local resolver
    pixel_injector_mode: str = "redroid"
    pixel_injector_local_share_scheme: str = "pixav-local://"

    # Strm-Resolver
    resolver_host: str = "0.0.0.0"
    resolver_port: int = 8000

    # Health endpoints (each worker exposes /health and /metrics on its port)
    maxwell_core_health_port: int = 8001
    media_loader_health_port: int = 8002
    pixel_injector_health_port: int = 8003
    sht_probe_health_port: int = 8004
    storage_worker_health_port: int = 8005
    vpn_sentinel_health_port: int = 8006
    health_host: str = "0.0.0.0"
    heartbeat_interval_seconds: int = 5
    heartbeat_stale_seconds: int = 120

    # Stash
    stash_url: str = "http://localhost:9999"
    stash_enabled: bool = False

    # Downloads
    download_dir: str = "./data/downloads"
    # Durable remuxes must live outside qBittorrent's content paths so torrent
    # cleanup cannot also delete the artifact referenced by PostgreSQL.
    remux_dir: str = "./data/remuxed"
    download_min_free_bytes: int = 100 * 1024**3
    download_min_free_percent: float = 10.0
    local_cleanup_success_hours: int = 24
    local_cleanup_failure_days: int = 7
    local_cleanup_batch_size: int = 100
    # Staging deletion stays off until an operator turns it on for the
    # deployment. Off, the janitor still evaluates and audits every artifact.
    local_cleanup_apply: bool = False
    # Storage activities need the Pixel-compatible guest and its Maestro flows.
    storage_flows_dir: str = "config/maestro/photos-canary"
    storage_readback_dir: str = "./data/storage-readback"
    # Prepared segments are hard-linked here under their canonical names and the
    # tools container mounts this root read-only. The prepared artifact keeps
    # its own path; only the link lives here.
    storage_staging_dir: str = "./data/storage-staging"
    storage_guest_data_dir: str = "./data/storage-guest"
    # The cold read-back container writes here. It stays under the project data
    # root so ``host_project_root`` can translate it for the Docker daemon.
    storage_readback_image: str = "pixav-storage-tools:1"
    # Where this process's project root lives on the Docker daemon's host. A
    # worker that runs on the host leaves it empty; a containerised worker sets
    # it, because bind-mount sources it hands the daemon are resolved on the
    # host and an untranslated container path silently mounts an empty dir.
    host_project_root: str = ""
    # The retained upload guest is keyed by its owner, so this identity has to
    # survive a restart. A fresh UUID each time would leave the previous guest
    # unclaimed and provision a second one carrying the same Google session.
    storage_worker_owner: str = ""

    # VPN egress detection (secondary to gluetun's kill switch, never a latch)
    # Empty URL disables the check: both observers must agree on one endpoint,
    # and a wrong or unreachable one would report "unknown" forever rather than
    # anything useful.
    vpn_egress_echo_url: str = ""
    vpn_egress_redis_key: str = "pixav:vpn:tunnel_ip"
    vpn_egress_interval_seconds: int = 300

    # Jackett
    jackett_url: str = "http://localhost:9117"
    jackett_api_key: str = ""

    # FlareSolverr
    flaresolverr_url: str = "http://localhost:8191"

    # Crawl
    # Format: "URL|tag1,tag2;URL2|tag3"
    crawl_seed_urls: str = "https://www.sehuatang.org/forum-103-1.html|sehuatang,hd"
    # Filter which internal links to visit from seed pages. Leave blank to visit everything.
    # Common forum patterns include either "thread-..." or "viewthread" style routes.
    crawl_link_filter_pattern: str = r"(viewthread|thread(-\d+)+\.html)"
    crawl_queries: str = ""
    crawl_max_pages: int = 50
    crawl_request_delay_seconds: float = 2.0
    crawl_max_board_pages: int = 3
    crawl_interval_seconds: int = 3600
    crawl_empty_error_threshold: int = 3
    crawl_empty_cycles_key: str = "pixav:crawl:empty_cycles"
    # Optional cookies for crawling (raw Cookie header or Netscape cookie file path).
    crawl_cookie_header: str = ""
    crawl_cookie_file: str = ""

    # Redis queue names
    queue_crawl: str = "pixav:crawl"
    queue_download: str = "pixav:download"
    queue_download_dlq: str = "pixav:download:dlq"
    queue_upload: str = "pixav:upload"
    queue_upload_dlq: str = "pixav:upload:dlq"

    # Retry
    download_max_retries: int = 6
    upload_max_retries: int = 6
    retry_backoff_seconds: str = "60,300,900,3600,21600,86400"
    dlq_retention_days: int = 30
    dlq_max_items_per_stage: int = 1000

    # Upload execution controls
    upload_max_concurrency: int = 1
    upload_lock_key: str = "pixav:upload:lock"
    upload_lock_ttl_seconds: int = 7200
    upload_task_timeout_seconds: int = 3600
    upload_ready_timeout_seconds: int = 120
    upload_verify_timeout_seconds: int = 300

    # How long a durable remote asset may go without an independent cold
    # re-read before the authority opens a verify-only execution for it. A
    # copy nobody ever looks at again is an assumption, not a fact. 0 disables
    # re-verification entirely.
    remote_reverify_interval_days: int = 30

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
