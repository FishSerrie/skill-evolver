# Agent Rules (cross-platform)

This file ensures all AI coding agents (Claude Code, Codex, OpenCode, etc.) follow the same project rules.

**Canonical source**: `.claude/CLAUDE.md` — this file is a pointer, not a copy.

## Git Workflow (mandatory)

1. **Never push directly.** All commits stay local; the user pushes manually.
2. **Branch-based development.** New tasks start on `feat/`, `fix/`, or `exp/` branches.
3. **Squash before merging to main.** Use `git reset --soft $(git merge-base HEAD main)` or `git rebase -i`.
4. **Always rebase onto main first** before squashing, to avoid overwriting mainline changes.
5. **main only accepts clean, squashed commits.** No development debris.
6. **User's manual commits are untouched.** Only squash agent-created commits.

## Commit Messages

```
<type>: <short description, max 72 chars>

- change 1
- change 2

Co-Authored-By: <agent/model name> <noreply@anthropic.com>
```

Types: feat / fix / refactor / docs / chore / test

## For full details

Read `.claude/CLAUDE.md` — it contains the complete SOP with examples and edge cases.
