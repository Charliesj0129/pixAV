---
name: fastapi-expert
description: Specialized knowledge for high-performance FastAPI applications in PixAV.
---

# FastAPI Expert

Best practices for `pixAV`'s FastAPI backend.

## Architecture

- **Clean Architecture**:
  - `routers/`: HTTP handling, validation.
  - `services/`: Business logic.
  - `repositories/`: Database access.
- **Dependency Injection**:
  - Use `Depends()` for ALL shared resources (DB session, config, current_user).
  - NEVER import global state directly in routes.

## Pydantic v2

- Use `model_validate()` instead of `from_orm()`.
- Use `Field(..., description="...")` for documentation.
- Use `ConfigDict` for configuration.

## Async Database (SQLAlchemy 2.0)

- Use `AsyncSession`.
- Use `select()`, `insert()`, `update()`, `delete()` constructs.
- **Commit**: `await session.commit()`.

## Background Tasks

- **Simple**: `BackgroundTasks` for non-critical tasks (e.g., log metrics).
- **Critical**: Redis Queue (Maxwell) for reliable jobs (e.g., video processing).

## Testing

- Use `AsyncClient` from `httpx`.
- Use `override_dependency` to mock DB/Auth in tests.
