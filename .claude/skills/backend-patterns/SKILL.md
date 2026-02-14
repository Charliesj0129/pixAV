---
name: backend-patterns
description: Backend architecture patterns, API design, database optimization, and server-side best practices.
---

# Backend Development Patterns

Architecture patterns for `pixAV`'s Python/FastAPI backend.

## Architecture Layers

### 1. **Routing Layer** (`service.py` / `routes.py`)

- **Responsibility**: HTTP request handling, validation, response formatting.
- **Do**: Delegate business logic to Service layer.
- **Don't**: Write SQL queries here.

### 2. **Service Layer** (`orchestrator.py`, `manager.py`)

- **Responsibility**: Business logic, transaction management, coordination.
- **Do**: Call Repositories for data access.
- **Don't**: Access `fastapi.Request` directly.

### 3. **Repository Layer** (`repository.py`)

- **Responsibility**: pure Data Access Object (DAO) pattern.
- **Do**: Interact with DB/Redis. Return domain models.
- **Don't**: Contain business logic.

## Asynchronous Patterns

### Background Tasks

- Use `fastapi.BackgroundTasks` for simple fire-and-forget.
- Use **Redis Queue (Maxwell)** for reliable async processing.

### Caching (Cache-Aside)

1. Check Redis for key `f"entity:{id}"`.
2. If hit, return JSON.loads(value).
3. If miss, fetch from DB.
4. Set Redis key with TTL (e.g., 300s).
5. Return entity.

## Database Optimization

- **N+1**: Avoid loops triggering queries. Fetch IDs -> Batch Query -> Map.
- **Projections**: Only select needed fields (`SELECT id, status FROM tasks`).
- **Indexes**: Ensure `WHERE` and `ORDER BY` columns are indexed.

## Error Handling

- Centralized exception handler in `main.py`.
- Custom exceptions: `TaskNotFoundError`, `QuotaExceededError`.
- Structured logging with `structlog`.
