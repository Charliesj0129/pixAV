---
name: code-reviewer
description: Structured code review process focusing on logic, security, and architectural fit.
---

# Code Review

Systematic review process for `pixAV`.

## The Protocol: READ -> UNDERSTAND -> VERIFY

### 1. READ (The "What")

- Read the changed code.
- Read the user request/ticket.
- Identify the _intent_ of the change.

### 2. UNDERSTAND (The "How")

- internalize the logic.
- Tracing execution paths.
- Ask: "Does this handle edge cases?" (Empty lists, None, network errors).
- Ask: "Is this secure?" (Injection, exposed secrets, permissions).

### 3. VERIFY (The "Proof")

- **Don't assume** it works.
- **Run the code**: `python -c "..."` or `pytest ...`.
- **Check types**: `mypy`.
- **Check style**: `ruff`.

## Review Checklist

- [ ] **Functional**: Does it do what was asked?
- [ ] **Security**: No secrets, valid inputs?
- [ ] **Performance**: No N+1 queries, efficient I/O?
- [ ] **Tests**: Are new tests added? Do they pass?
- [ ] **Docs**: Are docstrings/README updated?

## Feedback Style

- **Constructive**: "Consider using X for efficiency" vs "This is slow".
- **Specific**: Point to line numbers.
- **Actionable**: Suggest specific code changes.
