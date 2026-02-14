# Infrastructure — Code Map

## Docker Compose Services

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| postgres | pgvector/pgvector:pg16 | 5432 | Primary database (SSOT) |
| redis | redis:7-alpine | 6379 | Queue broker + cache |
| qbittorrent | linuxserver/qbittorrent | 8080 | Torrent client |
| jackett | linuxserver/jackett | 9117 | Torrent indexer proxy |
| flaresolverr | flaresolverr | 8191 | Cloudflare bypass |
| stash | stashapp/stash | 9999 | Metadata scraper |

**Redroid is NOT in compose** — ephemeral containers created/destroyed per task.

## Migrations

Located in `migrations/`, applied by `scripts/migrate.py`:

| File | Table |
|------|-------|
| 001 | `accounts` |
| 002 | `videos` |
| 003 | `tasks` |
| 004 | `storage_instances` |

## Dockerfiles

| File | Service |
|------|---------|
| `docker/pixel-injector.Dockerfile` | Upload worker |
| `docker/strm-resolver.Dockerfile` | Playback proxy |
| `docker/migrate.Dockerfile` | Migration runner |
