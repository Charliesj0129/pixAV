# Shared Module — Code Map

## Location
`src/pixav/shared/`

## Files

| File | Purpose |
|------|---------|
| `enums.py` | `TaskState`, `AccountStatus`, `VideoStatus`, `StorageHealth` |
| `models.py` | Frozen Pydantic models: `Account`, `Video`, `Task`, `StorageInstance` |
| `db.py` | asyncpg pool factory (`create_pool`) |
| `redis_client.py` | async Redis client factory (`create_redis`) |
| `queue.py` | `TaskQueue` — RPUSH/BLPOP/LLEN wrapper |
| `exceptions.py` | `PixavError` hierarchy (per-module error types) |
| `logging.py` | structlog JSON setup (`setup_logging`) |

## Key Patterns

- **All models are frozen** (`model_config = {"frozen": True}`) — use `model_copy(update={...})` for updates
- **Queue messages are JSON-serialized dicts** — not Pydantic models directly
- **Config** lives in `src/pixav/config.py` (Pydantic Settings, `PIXAV_` env prefix)
