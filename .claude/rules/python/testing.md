---
paths: ["**/*.py"]
---

# Python Testing

## Requirements

- **Framework**: `pytest` + `pytest-asyncio`.
- **Coverage**: Minimum 80% coverage required.
- **Mocks**: Use `unittest.mock` and `respx` for external services.

## Test Structure

- **Unit Tests**: Test individual functions/classes in isolation.
- **Integration Tests**: Test interactions between components (e.g., Service + Repo).
- **Fixtures**: Use `conftest.py` for shared fixtures.

## Best Practices

- **AAA Pattern**: Arrange, Act, Assert.
- **Descriptive Names**: `test_should_return_error_when_invalid_input`.
- **Markers**: Use `@pytest.mark.unit`, `@pytest.mark.integration`.
- **No Side Effects**: Tests must be independent and repeatable.
