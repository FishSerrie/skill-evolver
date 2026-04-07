#!/usr/bin/env bash
# Sync skill-evolver from Claude Code (plugin/) to Codex (.agents/)
# Handles platform-specific replacements automatically.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SRC="$REPO_ROOT/plugin/skills/skill-evolver"
DST="$REPO_ROOT/.agents/skills/skill-evolver"

echo "Syncing: Claude Code → Codex"
echo "  Source: $SRC"
echo "  Dest:   $DST"

# Clean destination
rm -rf "$DST"
mkdir -p "$DST/agents" "$DST/references" "$DST/scripts"

# ── SKILL.md: platform-specific replacements ──
sed \
  -e 's/^version: .*/metadata:\n  source: claude-port\n  version: 1.0.0\n  short-description: Autonomous skill evolution engine/' \
  -e 's/# Skill Evolver/# Skill Evolver (Codex)/' \
  -e 's|/skill-evolver |\$skill-evolver |g' \
  -e 's|`AskUserQuestion`|direct prompting|g' \
  -e 's|`claude -p`|the Codex CLI|g' \
  -e 's|claude -p |codex -q |g' \
  -e 's|通过 claude -p 子进程|通过 Codex CLI 子进程|g' \
  -e 's|claude CLI|Codex CLI|g' \
  "$SRC/SKILL.md" > "$DST/SKILL.md"

# ── Agents: copy as-is ──
cp "$SRC/agents/"*.md "$DST/agents/"

# ── References: copy as-is ──
cp "$SRC/references/"*.md "$DST/references/"

# ── Scripts: replace claude -p calls ──
for f in "$SRC/scripts/"*.py; do
  fname="$(basename "$f")"
  sed \
    -e 's|"claude", "-p"|"codex", "-q"|g' \
    -e 's|claude -p|codex -q|g' \
    -e 's|claude CLI|Codex CLI|g' \
    "$f" > "$DST/scripts/$fname"
done

cp "$SRC/scripts/__init__.py" "$DST/scripts/__init__.py" 2>/dev/null || true

TOTAL=$(find "$DST" -type f | wc -l | tr -d ' ')
echo "Done: $TOTAL files synced to .agents/"
