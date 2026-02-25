# Production Deployment Runbook

This document is the baseline for long-running `pixAV` deployments using Docker Compose.

## Goals

- Keep service processes auto-restarting (`restart: unless-stopped`)
- Reduce operational noise (log rotation)
- Avoid accidental secret commits
- Make upgrades repeatable and reversible
- Keep crawl reliability stable (especially `sehuatang` cookies + FlareSolverr)

## Deployment Files

- Base stack: `docker-compose.yml`
- Production override: `docker-compose.prod.yml`
- Runtime configuration: `.env` (create from `.env.example`; do not commit)

Use both compose files together:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

## Pre-Deploy Checklist

1. Create and review `.env` from `.env.example`
2. Replace all default passwords (`Postgres`, `qBittorrent`, `Stash`, etc.)
3. Provide valid `PIXAV_JACKETT_API_KEY` if search mode is used
4. Configure `PIXAV_CRAWL_COOKIE_FILE` for `sehuatang` crawling (Netscape cookie export recommended)
5. Confirm `PIXAV_FLARESOLVERR_URL` is reachable from the worker host
6. Validate health endpoints/ports for enabled services
7. Ensure Docker volumes are on persistent storage (not ephemeral disks)

## Cookie Handling (Sehuatang)

- Prefer `PIXAV_CRAWL_COOKIE_FILE=/absolute/path/sehuatang-cookies.txt`
- Netscape cookie exports are supported directly
- Required cookies typically include:
  - `cf_clearance`
  - `cPNj_2132_auth`
  - `_safe`
- Rotate cookies when crawling starts returning age-gate or challenge pages again

## Image Pinning Strategy

`docker-compose.prod.yml` allows overriding image tags via env vars:

- `PIXAV_IMAGE_POSTGRES`
- `PIXAV_IMAGE_REDIS`
- `PIXAV_IMAGE_QBITTORRENT`
- `PIXAV_IMAGE_JACKETT`
- `PIXAV_IMAGE_FLARESOLVERR`
- `PIXAV_IMAGE_STASH`

Recommended practice:

1. Pin exact tags or digests in your deployment environment
2. Upgrade one component at a time
3. Run smoke tests after each upgrade

## First Deployment

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d postgres redis
uv run python scripts/migrate.py
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

## Operational Checks

Run after deploy and after upgrades:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail=200
```

Application checks:

- `sht_probe` can crawl seed URLs and enqueue jobs
- `media_loader` can connect to qBittorrent
- `pixel_injector` can reach Docker socket / ADB target
- `strm_resolver` health endpoint responds

## Backups and Retention

Back up these volumes regularly:

- `pgdata` (critical)
- `redis-data` (optional but useful for queue recovery)
- `qbit-config`
- `jackett-config`
- `stash-config`
- `./data/stash`

Minimum recommendation:

1. Daily PostgreSQL backup
2. Before any schema migration or image upgrade, take an extra snapshot
3. Test restore on a non-production host periodically

## Logging and Disk Hygiene

`docker-compose.prod.yml` enables JSON log rotation to prevent unbounded log growth.

Also monitor:

- `./data/downloads` growth
- qBittorrent completed/failed payloads
- Docker image/cache buildup (`docker system df`)

## Safe Upgrade Procedure

1. Backup data/volumes
2. Pull new images (or update pinned tags)
3. Run DB migrations if needed
4. Restart a subset of services first (`postgres`, `redis`, infra)
5. Restart workers/services gradually
6. Verify queue movement and health endpoints
7. Monitor logs for at least one crawl cycle

## Repository Hygiene (Long-Term)

The repo now ignores common local probe files (`/test_*.py`, `/test_*.js`, `node_modules/`) so local debugging does not pollute commit diffs.

For team usage:

1. Keep exploratory scripts under `scripts/` or a dedicated ignored scratch directory
2. Do not commit cookie files or `.env`
3. Prefer adding reproducible tests under `tests/` instead of root-level probes
