# Strm-Resolver — Code Map

## Location
`src/pixav/strm_resolver/`

## Purpose
FastAPI service that resolves Google Photos share URLs to direct CDN streaming URLs.

## Files

| File | Purpose |
|------|---------|
| `app.py` | FastAPI factory (`create_app`) |
| `routes.py` | `GET /resolve/{id}`, `GET /stream/{id}` (302), `GET /health` |
| `resolver.py` | `GooglePhotosResolver` — share URL → CDN URL (stub) |
| `cache.py` | `CdnCache` — Redis TTL cache (55min TTL for ~1hr CDN URLs) |
| `middleware.py` | Rate limiting (stub), CORS setup |

## Endpoints

| Method | Path | Response |
|--------|------|----------|
| GET | `/resolve/{video_id}` | `{"cdn_url": "..."}` |
| GET | `/stream/{video_id}` | 302 redirect to CDN URL |
| GET | `/health` | `{"status": "ok"}` |
