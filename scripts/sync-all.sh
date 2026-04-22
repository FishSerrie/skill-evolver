#!/usr/bin/env bash
# Sync all platforms from the Claude Code source of truth.
# Run this after modifying plugin/skills/skill-evolver/ to keep all platforms in sync.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Syncing all platforms ==="
echo ""

bash "$SCRIPT_DIR/sync-opencode.sh"
echo ""

bash "$SCRIPT_DIR/sync-codex.sh"
echo ""

echo "=== All platforms synced ==="
