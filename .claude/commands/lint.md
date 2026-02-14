# Python Linter

Run Python code linting and formatting tools.

## Purpose

This command helps you maintain code quality using Python's best linting and formatting tools.

## Usage

```
/lint
```

## What this command does

1. **Runs modern toolchain** (ruff, black, isort)
2. **Provides detailed feedback** on code quality issues
3. **Auto-fixes formatting** where possible
4. **Checks type hints** if mypy is configured

## Example Commands

### Ruff (linter & formatter)

```bash
# Lint all Python files
ruff check .

# Lint with auto-fix
ruff check --fix .

# Lint specific file
ruff check src/main.py
```

### Black (code formatting)

```bash
# Format all Python files
black .

# Check formatting without changing files
black --check .
```

### isort (import sorting)

```bash
# Sort imports in all files
isort .
```

### mypy (type checking)

```bash
# Check types in all files
mypy .
```

## Configuration Files

### pyproject.toml

```toml
[tool.ruff]
line-length = 120
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "N", "S", "C901"]
ignore = ["E501"]
```

## Best Practices

- Run linters before committing code
- Use `ruff check --fix` to auto-resolve common issues
- Fix linting issues promptly
- Use type hints for better code documentation
