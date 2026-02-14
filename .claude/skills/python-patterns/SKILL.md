---
name: python-patterns
description: Pythonic idioms, PEP 8 standards, type hints, and best practices for building robust, efficient, and maintainable Python applications.
---

# Python Development Patterns

Idiomatic Python patterns and best practices for building robust, efficient, and maintainable applications.

## Core Principles

### 1. Readability Counts

- Use clear, descriptive variable names.
- Avoid "clever" one-liners that obscure logic.

### 2. Explicit is Better Than Implicit

- Avoid magic side effects.
- Use explicit configuration patterns (e.g., `pydantic-settings`).

### 3. Type Hints

- **Modern Syntax**: Use `list[str]`, `dict[str, int]` (Python 3.9+).
- **Protocols**: Use `typing.Protocol` for structural typing (duck typing).
- **Generics**: Use `TypeVar` for flexible functions.

## Error Handling

- **Specific Exceptions**: Catch specific errors, never bare `except:`.
- **Chaining**: Use `raise ValueError(...) from e` to preserve stack traces.
- **Custom Exceptions**: Define domain-specific exception hierarchies.

## Concurrency (Async/Await)

- **I/O Bound**: Use `asyncio` for DB, Network, File I/O.
- **Non-blocking**: Verify libraries are async-compatible (e.g., `asyncpg`, `httpx`).
- **Gather**: Use `asyncio.gather` for concurrent execution.

## Data Structures

- **Pydantic**: Use `BaseModel` for all data transfer objects (DTOs) and validation.
- **Dataclasses**: Use `@dataclass(frozen=True)` for internal immutable state.
- **Enums**: Use `StrEnum` (Python 3.11+) or `Enum` for fixed sets of values.

## Tooling Integration

- **Format**: `uv run black .`
- **Lint**: `uv run ruff check .`
- **Type Check**: `uv run mypy .`
