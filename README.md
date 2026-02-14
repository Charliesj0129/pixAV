# pixAV

Maxwell's Demon distributed media pipeline scaffold.

## Architecture

`pixAV` is split into five modules connected by Redis queues, with PostgreSQL as the source of truth:

1. `sht_probe` - crawler and magnet discovery (stub)
2. `media_loader` - torrent download + remux (stub)
3. `pixel_injector` - Redroid-driven Google Photos upload (MVP skeleton)
4. `maxwell_core` - scheduling, dispatch, backpressure, GC (stub)
5. `strm_resolver` - FastAPI playback resolver/proxy (skeleton)

All services are Python-first prototypes under `src/pixav/`. Rust workspace stubs for future rewrites live under `rust/`.

## Project Layout

```text
src/pixav/            # Python modules
migrations/           # PostgreSQL schema migrations
scripts/              # migrate/seed scripts
docker/               # service Dockerfiles
docs/CODEMAPS/        # architecture maps
docs/adr/             # architecture decision records
tests/                # pytest suite
```

## Quickstart

1. Install dependencies:
```bash
uv sync
```

2. Start infrastructure:
```bash
docker compose up -d postgres redis
```

3. Apply schema:
```bash
uv run python scripts/migrate.py
```

4. Run tests:
```bash
uv run pytest
```

5. Run strm-resolver API:
```bash
uv run uvicorn pixav.strm_resolver.app:create_app --factory --host 0.0.0.0 --port 8000
```

## Queue Names

- `pixav:crawl`
- `pixav:download`
- `pixav:upload`
- `pixav:verify`

## Notes

- Redroid is intentionally **not** in `docker-compose.yml`; containers are created per task by `pixel_injector`.
- Shared contracts live in `src/pixav/shared/` and should be stabilized before any Rust rewrites.
