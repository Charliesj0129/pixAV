---
name: coding-standards
description: Universal coding standards and best practices (KISS, DRY, Immutability) adapted for PixAV.
---

# Coding Standards

Core principles for `pixAV` development.

## 1. Core Principles

### Readability First

Code is read more than written. Optimize for clarity.

- **Good**: `calculate_video_duration(video_path)`
- **Bad**: `calc_dur(v)`

### KISS (Keep It Simple, Stupid)

- Avoid over-engineering.
- Don't build generic solutions for one-off problems.

### DRY (Don't Repeat Yourself)

- Extract common logic to `src/pixav/shared` or `utils`.
- Don't duplicate Pydantic models; use inheritance/composition.

### Immutability

- Prefer immutable data structures (`frozen=True` dataclasses, Pydantic models).
- Return **new** objects instead of mutating arguments.

## 2. Python Specifics

- **Type Hints**: Mandatory for all function signatures.
- **Docstrings**: Required for public modules/classes/functions.
- **Formatting**: `black` / `ruff` compliant.

## 3. Code Organization

- **Feature-based Packaging**: Group by feature (`media_loader`), not layer (`controllers`).
- **Small Interfaces**: Keep interfaces focused (Interface Segregation).
- **Dependency Injection**: Pass dependencies explicitly (e.g., `client` passed to `Service`).
