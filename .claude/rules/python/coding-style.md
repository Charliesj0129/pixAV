---
paths: ["**/*.py"]
---

# Python Coding Style

## Standards

- **PEP 8**: Follow PEP 8 guidelines.
- **Line Length**: 120 characters max.
- **Type Hints**: Mandatory on all public functions/methods.
- **Imports**: Sorted by `isort` (std lib > 3rd party > local).

## Modern Python Idioms

- **Data Classes**: Use `@dataclass(frozen=True)` or Pydantic `BaseModel`.
- **Type Checking**: Use `mypy` strict mode where possible.
- **Path Handling**: Use `pathlib.Path` instead of `os.path`.
- **String Formatting**: Use f-strings.

## Logging

- Use `structlog` or standard `logging`.
- **NEVER** use `print()` in production code.

## Async/Await

- Use `asyncio` for I/O bound operations.
- Avoid blocking calls in async functions.
