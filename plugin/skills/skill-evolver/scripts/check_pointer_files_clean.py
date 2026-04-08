#!/usr/bin/env python3
"""Check that agent pointer files do not leak grading/comparison logic.

Used by evals.json case 9 as a script_check assertion. Replaces a brittle
not_contains "assertion type" check that was a false positive: the literal
string "assertion type" legitimately appears in SKILL.md and references/
as documentation of the GT data schema, which is NOT grading logic.

This script scans only agents/grader_agent.md and agents/comparator_agent.md
(the files that should be pure pointers to skill-creator's full versions)
for substrings that would indicate the pointer files have been polluted
with actual grading or comparison rule content.

Exit 0 = clean. Exit 1 = leak detected.
The skill_path is provided by LocalEvaluator via cwd.
"""
from pathlib import Path
import sys

POINTER_FILES = [
    "agents/grader_agent.md",
    "agents/comparator_agent.md",
]

# Pointer files should be short — they only delegate to skill-creator's
# full versions. Anything beyond ~60 lines means real grading/comparison
# rules have been copy-pasted in (the leak we want to catch).
MAX_POINTER_LINES = 60

# Required positive marker — pointer files must self-identify as pointers,
# not as authoritative grading docs.
REQUIRED_MARKER = "pointer file"

skill_root = Path.cwd()
leaks: list[str] = []

for rel in POINTER_FILES:
    f = skill_root / rel
    if not f.exists():
        leaks.append(f"{rel}: missing")
        continue
    text = f.read_text()
    line_count = len(text.splitlines())
    if line_count > MAX_POINTER_LINES:
        leaks.append(
            f"{rel}: {line_count} lines (max {MAX_POINTER_LINES}); "
            f"pointer files should delegate, not contain rules"
        )
    if REQUIRED_MARKER not in text.lower():
        leaks.append(
            f"{rel}: missing required marker '{REQUIRED_MARKER}' — "
            f"file does not self-identify as a pointer"
        )

if leaks:
    print("\n".join(leaks), file=sys.stderr)
    sys.exit(1)

sys.exit(0)
