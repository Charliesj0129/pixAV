---
name: git-automation
description: Automates Git workflows including commit message generation, branch management, and PR creation.
---

# Git Automation

Streamline Git operations in `pixAV`.

## Workflows

### 1. Smart Commit

When asked to "commit changes":

1.  **Analyze**: Run `git diff --cached`.
2.  **Summarize**: Generate a conventional commit message (e.g., `feat(parser): add resiliency to jackett client`).
3.  **Execute**: `git commit -m "..."`.

### 2. PR Creation (GitHub)

When asked to "create a PR":

1.  **Push**: `git push -u origin <current_branch>`.
2.  **Draft**: Create a title and body based on the diff.
3.  **Open**: Use `gh pr create` (if available) or output the URL.

### 3. Branch Management

- **Feature Branches**: `feat/description-of-feature`
- **Fix Branches**: `fix/description-of-bug`
- **Cleanup**: `git branch -d <merged_branch>`

## Best Practices

- **Atomic Commits**: Commit one logical change at a time.
- **Verification**: Run tests _before_ pushing.
