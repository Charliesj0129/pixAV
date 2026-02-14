---
name: security-review
description: Comprehensive security checklist and patterns for PixAV details secrets, input validation, and secure handling.
---

# Security Review Skill

Security best practices for `pixAV`.

## 1. Secrets Management

- **NO** hardcoded secrets (API keys, passwords).
- **ALWAYS** use `pydantic-settings` to load from env vars.
- **VERIFY** `.env` is gitignored.

## 2. Input Validation

- **Boundary Validation**: Validate all inputs at API entry (Pydantic).
- **File Uploads**: Restrict size and type. Verify magic numbers, not just extensions.
- **Sanitization**: Sanitize filenames before saving.

## 3. Injection Prevention

- **SQL**: Use SQLAlchemy/asyncpg query parameters. NEVER string concatenation.
- **Shell**: Avoid `shell=True` in subprocesses. Use list args (`["ls", "-l"]`).

## 4. Authentication (Future)

- Use HTTP-only cookies for tokens.
- Implement Rate Limiting on login endpoints.

## 5. Docker Security

- Run containers as non-root (`user: 1000:1000`).
- Scan images for vulnerabilities (`trivy`).
- Don't expose ports unnecessarily (use internal Docker network).

## 6. Dependency Management

- Pin versions in `pyproject.toml`.
- Periodically run `uv run safety check` (or similar).

## Checklist

- [ ] Secrets from Env?
- [ ] Pydantic models for all inputs?
- [ ] No `shell=True`?
- [ ] No `eval()` or `exec()`?
- [ ] Logging does not leak secrets?
