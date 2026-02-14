---
name: tdd-workflow
description: Enforces test-driven development Workflow: Red -> Green -> Refactor.
---

# TDD Workflow

Strict workflow for implementing features in `pixAV`.

## The Cycle

1.  **RED**: Write a failing test.
    - Create `tests/feature/test_scenario.py`.
    - Define the expected interface and behavior.
    - Run `uv run pytest tests/feature/test_scenario.py`.
    - CONFIRM it fails (e.g., `ImportError` or `AssertionError`).

2.  **GREEN**: Make it pass.
    - Implement the _minimal_ code required.
    - Don't over-engineer.
    - Run the test again.
    - CONFIRM it passes.

3.  **REFACTOR**: Clean up.
    - Optimize code structure.
    - Improve variable names.
    - Remove duplication.
    - Run tests again to ensure NO regression.

## Checklist

- [ ] Requirements clarity before coding.
- [ ] Test covers happy path.
- [ ] Test covers edge cases (empty input, errors).
- [ ] Code is formatted (`ruff check`).
- [ ] Types are checked (`mypy`).

## Tools

- `pytest --watch`: Run tests on file change (requires `pytest-watch` or similar).
- `pytest -k "test_name"`: Run specific test.
- `pytest --pdb`: Enter debugger on failure.
