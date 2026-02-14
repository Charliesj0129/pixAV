# Security Guidelines

## Mandatory Security Checks

Before ANY commit:

- [ ] No hardcoded secrets (API keys, passwords, tokens)
- [ ] All user inputs validated/sanitized
- [ ] SQL injection prevention (use ORM/parameterized queries)
- [ ] SSRF prevention (validate URLs)
- [ ] Rate limiting on external calls
- [ ] Error messages don't leak sensitive data

## Secret Management

- **NEVER** hardcode secrets in source code.
- **ALWAYS** use `pydantic-settings` to load from env vars.
- **VERIFY** `.env` is gitignored.

## Input Validation

- Validate all inputs at the system boundary (API, CLI, Queue).
- Use Pydantic models for strict schema validation.
