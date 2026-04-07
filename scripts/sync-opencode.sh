#!/usr/bin/env bash
# Sync skill-evolver from Claude Code (plugin/) to OpenCode (.opencode/)
# Handles platform-specific replacements automatically.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SRC="$REPO_ROOT/plugin/skills/skill-evolver"
DST="$REPO_ROOT/.opencode/skills/skill-evolver"

echo "Syncing: Claude Code → OpenCode"
echo "  Source: $SRC"
echo "  Dest:   $DST"

# Clean destination (preserve directory structure)
rm -rf "$DST"
mkdir -p "$DST/agents" "$DST/references" "$DST/scripts"

# ── SKILL.md: platform-specific replacements ──
sed \
  -e 's/^version: .*/compatibility: opencode\nmetadata:\n  source: claude-port\n  version: 1.0.0/' \
  -e 's/# Skill Evolver/# Skill Evolver (OpenCode)/' \
  -e 's|/skill-evolver |/skill-evolver |g' \
  -e 's|`AskUserQuestion`|`question`|g' \
  -e 's|`claude -p`|the LLM CLI|g' \
  -e 's|claude -p |llm-cli |g' \
  -e 's|通过 claude -p 子进程|通过 LLM CLI 子进程|g' \
  -e 's|claude CLI|LLM CLI|g' \
  "$SRC/SKILL.md" > "$DST/SKILL.md"

# ── Agents: copy as-is (no platform-specific content) ──
cp "$SRC/agents/"*.md "$DST/agents/"

# ── References: copy as-is ──
cp "$SRC/references/"*.md "$DST/references/"

# ── Scripts: replace claude -p calls ──
for f in "$SRC/scripts/"*.py; do
  fname="$(basename "$f")"
  sed \
    -e 's|"claude", "-p"|"llm", "run"|g' \
    -e 's|claude -p|llm run|g' \
    -e 's|claude CLI|LLM CLI|g' \
    "$f" > "$DST/scripts/$fname"
done

# Copy __init__.py as-is
cp "$SRC/scripts/__init__.py" "$DST/scripts/__init__.py" 2>/dev/null || true

TOTAL=$(find "$DST" -type f | wc -l | tr -d ' ')
echo "Done: $TOTAL files synced to .opencode/"
