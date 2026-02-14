# CLAUDE.md

Guidance for agents working in `pixAV`.

## Project Overview

`pixAV` (Maxwell's Demon) is a distributed media pipeline built as Python microservices communicating over Redis queues with PostgreSQL as the single source of truth. The architecture follows a staged pipeline pattern: crawl → download → upload → verify → resolve.

Rust crates are present as future rewrite targets — do not modify Rust code unless explicitly asked.

## Architecture

```
SHT-Probe → Media-Loader → Maxwell-Core → Pixel-Injector → STRM-Resolver
(crawl)      (download)     (orchestrate)   (upload)         (resolve)
```

### Modules

| Module             | Path                        | Responsibility                                                                                |
| ------------------ | --------------------------- | --------------------------------------------------------------------------------------------- |
| **sht_probe**      | `src/pixav/sht_probe/`      | Crawl sites via FlareSolverr, parse HTML, extract magnets                                     |
| **media_loader**   | `src/pixav/media_loader/`   | Download torrents via qBittorrent, remux with FFmpeg, scrape metadata from Stash              |
| **maxwell_core**   | `src/pixav/maxwell_core/`   | LRU account scheduling, task dispatching, backpressure monitoring, orphan cleanup             |
| **pixel_injector** | `src/pixav/pixel_injector/` | ADB connection, Redroid container management, file upload, Google Photos verification         |
| **strm_resolver**  | `src/pixav/strm_resolver/`  | FastAPI service resolving Google Photos share URLs to CDN streaming URLs with Redis cache     |
| **shared**         | `src/pixav/shared/`         | Enums, domain models (`Video`, `Task`), DB/Redis wrappers, queue helpers, logging, exceptions |

### Queue Names

- `pixav:crawl` — new URLs for SHT-Probe
- `pixav:download` — magnets for Media-Loader
- `pixav:upload` — files for Pixel-Injector
- `pixav:verify` — share URLs for verification

### Key Infrastructure

- **PostgreSQL** — SSOT for `videos` and `tasks` tables
- **Redis** — task queues (`BRPOPLPUSH` pattern) and CDN URL cache
- **Redroid** — ephemeral Android containers, created/destroyed per task (NOT in docker-compose)
- **Docker Compose** — postgres, redis, and service containers

## Essential Commands

```bash
# Setup
uv sync                                    # Install all dependencies

# Testing
uv run pytest                              # Run all tests
uv run pytest --cov=src/pixav --cov-report=term-missing  # With coverage
uv run pytest tests/media_loader/ -v       # Run specific module tests

# Linting & Formatting
uv run ruff check src tests                # Lint (replaces flake8, 10-100x faster)
uv run ruff check --fix src tests          # Lint with auto-fix
uv run black src tests scripts             # Format code
uv run isort src tests scripts             # Sort imports
uv run mypy src                            # Type checking

# Database
docker compose up -d postgres redis        # Start infrastructure
uv run python scripts/migrate.py           # Run migrations
uv run python scripts/seed.py             # Seed test data

# Docker
docker compose up -d                       # Start all services
docker compose logs -f postgres redis      # Follow infrastructure logs
docker compose down -v                     # Tear down with volumes
```

## Security Guidelines

### ⛔ NEVER Hardcode Secrets

```python
# ❌ WRONG
DB_URL = "postgresql://user:pass@host/db"

# ✅ CORRECT — use pydantic-settings
from pixav.config import Settings
settings = Settings()  # loads from environment / .env
```

**Rules:**

- Use `pydantic-settings` (`Settings` class in `config.py`) for all config
- Load secrets from environment variables or `.env` file
- Verify `.env` is in `.gitignore`
- Never log passwords, tokens, or connection strings

## Code Standards

### Python Style

- **PEP 8** with `line-length = 120`
- **Type annotations** on all public function signatures
- **Immutability** — use `model_copy(update={...})` for Pydantic models, `@dataclass(frozen=True)` for DTOs
- **Formatting** — `black` for code, `isort` for imports, `ruff` for linting
- **Logging** — use `structlog` / `logging`, never `print()` in production paths

### File Organization

- Keep files under 400 lines (800 max)
- Organize by feature/domain, not by type
- Extract utilities from large modules

### Error Handling

- Handle errors explicitly — never silently swallow exceptions
- Use bare `except` only with immediate re-raise
- Log detailed context on failure
- Validate all external data at system boundaries

### Naming Conventions

- Files: `snake_case.py`
- Classes: `PascalCase`
- Functions/variables: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Protocols for interfaces (duck typing)

### Design Patterns

- **Protocol** (duck typing) for interfaces — see `media_loader/interfaces.py`
- **Repository** pattern for database access — see `shared/repository.py`
- **Pydantic models** as domain objects — see `shared/models.py`
- **Enums** for state machines — see `shared/enums.py`

## Testing

**Target: 80%+ coverage** (current: 77%)

```bash
uv run pytest --cov=src/pixav --cov-report=term-missing
```

- **Framework**: pytest with pytest-asyncio
- **Mocking**: `unittest.mock` + `respx` for HTTP
- **Async**: `asyncio_mode = "auto"` — all async tests run automatically
- **Markers**: Use `@pytest.mark.unit` / `@pytest.mark.integration`
- **Fixtures**: Prefer factory functions over complex fixtures

## Common Issues

**Import errors in tests**

- `pythonpath = ["src"]` is set in `pyproject.toml` — imports use `from pixav.module import ...`

**Redroid container issues**

- Redroid is NOT in docker-compose — created/destroyed per task via Docker SDK
- ADB connects via `asyncio.subprocess`, NOT `uiautomator2` directly

**Queue contract changes**

- Keep queue payloads backward-compatible
- Always include `video_id` as UUID string in queue messages

## Documentation

- ADRs: `docs/adr/`
- Code maps: `docs/CODEMAPS/`

## Agent Usage

When working on this project, prefer these agents:

- **Complex features** → use `planner` agent first
- **After writing code** → use `code-reviewer` or `python-reviewer`
- **New features** → use `tdd-guide` (write tests first)
- **Before commits** → use `security-reviewer`
- **Build failures** → use `build-error-resolver`
