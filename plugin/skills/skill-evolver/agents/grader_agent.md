# Grader Agent

> **This is a pointer file. The actual grading protocol lives in skill-creator.**

## Protocol Source

The full grading protocol is located in the skill-creator installation directory:

```
<creator-path>/agents/grader.md
```

When grading, you MUST read Creator's `agents/grader.md` for the complete grading rules and output format.

## Usage

```python
from common import get_creator_agent_path
grader_path = get_creator_agent_path("grader.md")
# Read grader_path content as the grading protocol
```

## Why no copy is kept here

- Creator is officially maintained; the grading protocol evolves with Creator updates
- Keeping a copy here would cause version drift (Creator updates while Evolver's copy stays stale)
- skill-creator is a hard dependency of skill-evolver — there is no "Creator unavailable" scenario

## If you see this file but Creator is not installed

Install skill-creator first. See the "Prerequisites" section in SKILL.md.
