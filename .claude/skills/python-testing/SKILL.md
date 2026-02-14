---
name: python-testing
description: Python testing strategies using pytest, TDD methodology, fixtures, mocking, and coverage requirements.
---

# Python Testing Patterns

Comprehensive testing strategies for Python applications using pytest.

## Core Philosophy

- **TDD Requirement**: Write tests BEFORE code (Red -> Green -> Refactor).
- **Coverage Target**: 80%+ overall, 100% for critical paths.

## pytest Fundamentals

### Fixtures

- Use `conftest.py` for shared fixtures.
- Prefer `yield` fixtures for setup/teardown.
- Use `pytest-asyncio` for async fixtures.

### Assertions

- Use simple `assert` statements.
- Use `pytest.raises(Error, match="...")` for exceptions.

### Mocks

- Use `unittest.mock.AsyncMock` for async functions.
- Use `respx` for mocking HTTP requests (httpx).
- **Avoid** mocking internal implementation details; mock boundaries.

## Testing Async Code

```python
import pytest

@pytest.mark.asyncio
async def test_async_function():
    result = await async_op()
    assert result == "success"
```

## Categorization

- Use markers to categorize tests:
  - `@pytest.mark.unit`: Fast, isolated tests.
  - `@pytest.mark.integration`: Tests with DB/Redis/Docker.
  - `@pytest.mark.slow`: Long-running tests.

## Running Tests

```bash
uv run pytest -v                 # Run all
uv run pytest -m unit            # Run unit tests
uv run pytest --cov=src/pixav    # Check coverage
```
