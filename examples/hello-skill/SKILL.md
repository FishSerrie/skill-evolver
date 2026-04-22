---
name: code-review-helper
description: "Reviews Python code snippets and provides improvement suggestions. Triggers on: 'review this code', 'code review', 'check my Python'."
---

# Code Review Helper

A simple skill that reviews Python code snippets and suggests improvements.

## What You Do

When the user shares Python code for review, you should:

1. Read the code carefully
2. Identify issues in these categories:
   - Bug risks (potential runtime errors, edge cases)
   - Style issues (naming, formatting)
   - Performance concerns
3. Provide specific, actionable suggestions

## Output Format

For each issue found, use this format:

```
**[Category]** Line N: Description of the issue
  Suggestion: How to fix it
```

## Rules

- Be specific — reference exact line numbers and variable names
- Prioritize bugs over style issues
- If the code is clean, say so briefly — do not invent issues
- Keep suggestions concise (one sentence each)
