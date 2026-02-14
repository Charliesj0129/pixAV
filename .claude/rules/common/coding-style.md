# Coding Style

## Immutability (CRITICAL)

ALWAYS create new objects, NEVER mutate existing ones:

- **WRONG**: `modify(original, field, value)` → changes in-place
- **CORRECT**: `update(original, field, value)` → returns new copy

Rationale: Immutable data prevents hidden side effects, makes debugging easier, and enables safe concurrency.

## File Organization

- **Small Files**: Keep files under 400 lines (800 max).
- **Cohesion**: Organize by feature/domain, not by type.
- **Utilities**: Extract helpers from large modules.

## Error Handling

- **Explicit**: Handle errors explicitly; never silently swallow exceptions.
- **Context**: Log detailed context on failure (use `structlog`).
- **Boundaries**: Validate all external data at system boundaries.

## Naming

- **Descriptive**: Use full words (e.g., `repository`, not `repo`).
- **Consistent**: Follow project conventions (`snake_case` for Python).
