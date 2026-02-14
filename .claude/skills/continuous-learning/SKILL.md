---
name: continuous-learning
description: Pattern to extract learnings from sessions and update the project knowledge base.
---

# Continuous Learning

Methodology for improving `pixAV` development over time.

## 1. Post-Task Review

At the end of a complex task, ask:

- "What project-specific patterns did I learn?"
- "Did I encounter any 'gotchas' with Redroid/Jackett?"

## 2. Rule Updates

If a new convention is established:

- Update `.claude/rules/`.
- Example: "Always use `aenter`/`aexit` for async context managers."

## 3. Skill Enhancements

If a specific workflow (e.g., "Debugging Redroid Connection") is repeated:

- Create a new skill or feature in `task_boundary`.
- Document the exact steps.

## 4. Knowledge Graph

- Record key architectural decisions.
- Note "dead ends" to avoid repeating mistakes.
