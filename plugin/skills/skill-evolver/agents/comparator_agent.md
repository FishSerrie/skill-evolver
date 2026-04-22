# Comparator Agent

> **This is a pointer file. The actual comparison protocol lives in skill-creator.**

## Protocol Source

The full blind comparison protocol is located in the skill-creator installation directory:

```
<creator-path>/agents/comparator.md
```

When performing A/B comparisons, you MUST read Creator's `agents/comparator.md` for the complete comparison rules and output format.

## Usage

```python
from common import get_creator_agent_path
comparator_path = get_creator_agent_path("comparator.md")
# Read comparator_path content as the comparison protocol
```

## Why no copy is kept here

- Creator is officially maintained; the comparison protocol evolves with Creator updates
- Keeping a copy here would cause version drift
- skill-creator is a hard dependency of skill-evolver — there is no "Creator unavailable" scenario

## If you see this file but Creator is not installed

Install skill-creator first. See the "Prerequisites" section in SKILL.md.
