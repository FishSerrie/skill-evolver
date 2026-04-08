#!/usr/bin/env bash
# Unified installer for skill-evolver — handles Claude Code, Codex, and OpenCode.
#
# Usage:
#   bash scripts/install.sh --claude    [--global|--project] [--dry-run]
#   bash scripts/install.sh --codex     [--global|--project] [--dry-run]
#   bash scripts/install.sh --opencode  [--global|--project] [--dry-run]
#   bash scripts/install.sh --all       [--global|--project] [--dry-run]
#
# Defaults: --global (installs to the user-wide skills directory).
#
# For Codex and OpenCode, this script first runs the relevant sync-*.sh to
# regenerate the platform-specific variant under .agents/ or .opencode/, then
# copies it to the install target. For Claude Code, it copies plugin/skills/
# skill-evolver directly.
#
# Claude Code users who prefer `/plugin install` can skip this script entirely
# and follow README § Claude Code Quick Start → Option A instead.

set -euo pipefail

PLATFORM=""
SCOPE="global"
DRY_RUN=0

usage() {
  sed -n '2,20p' "$0"
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --claude)   PLATFORM="claude" ;;
    --codex)    PLATFORM="codex" ;;
    --opencode) PLATFORM="opencode" ;;
    --all)      PLATFORM="all" ;;
    --global)   SCOPE="global" ;;
    --project|--local) SCOPE="project" ;;
    --dry-run)  DRY_RUN=1 ;;
    -h|--help)  usage 0 ;;
    *)
      echo "Error: unknown argument '$1'" >&2
      usage 1
      ;;
  esac
  shift
done

if [[ -z "$PLATFORM" ]]; then
  echo "Error: specify one of --claude, --codex, --opencode, --all" >&2
  usage 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_PLUGIN="$REPO_ROOT/plugin/skills/skill-evolver"

if [[ ! -d "$SRC_PLUGIN" ]]; then
  echo "Error: source not found at $SRC_PLUGIN" >&2
  echo "       Are you running this from a clean clone of skill-evolver?" >&2
  exit 1
fi

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

run() {
  # run "<description>" command args...
  local desc="$1"
  shift
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] $desc"
    printf '           $'
    printf ' %q' "$@"
    printf '\n'
  else
    echo "→ $desc"
    "$@"
  fi
}

clean_copy() {
  # clean_copy <src> <dst>
  local src="$1" dst="$2"
  run "mkdir parent of $dst" mkdir -p "$(dirname "$dst")"
  if [[ -e "$dst" || -L "$dst" ]]; then
    run "remove existing $dst" rm -rf "$dst"
  fi
  run "copy $src → $dst" cp -R "$src" "$dst"
}

# ─────────────────────────────────────────────
# Per-platform installers
# ─────────────────────────────────────────────

install_claude() {
  echo ""
  echo "=== Installing Claude Code skill ==="
  local dst
  if [[ "$SCOPE" == "global" ]]; then
    dst="$HOME/.claude/skills/skill-evolver"
  else
    dst="$PWD/.claude/skills/skill-evolver"
  fi
  clean_copy "$SRC_PLUGIN" "$dst"
  echo "✓ Claude Code skill installed at: $dst"
  echo "  Restart Claude Code, then run: /skill-evolver"
}

install_codex() {
  echo ""
  echo "=== Installing Codex skill ==="
  # 1. Regenerate .agents/ mirror from plugin/ via sync-codex.sh
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] regenerate .agents/ from plugin/"
    echo "           \$ bash $SCRIPT_DIR/sync-codex.sh"
  else
    bash "$SCRIPT_DIR/sync-codex.sh"
  fi

  local src_mirror="$REPO_ROOT/.agents/skills/skill-evolver"
  local dst
  if [[ "$SCOPE" == "global" ]]; then
    dst="$HOME/.agents/skills/skill-evolver"
  else
    # Project-local: sync-codex.sh already created it in .agents/ — nothing
    # to copy, just report.
    echo "✓ Codex skill installed at: $src_mirror (project-local)"
    echo "  Launch Codex from this directory; it auto-discovers .agents/skills/"
    return
  fi
  clean_copy "$src_mirror" "$dst"
  echo "✓ Codex skill installed at: $dst"
  echo "  Launch Codex; use \$skill-evolver mention syntax in your prompt"
}

install_opencode() {
  echo ""
  echo "=== Installing OpenCode skill ==="
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] regenerate .opencode/ from plugin/"
    echo "           \$ bash $SCRIPT_DIR/sync-opencode.sh"
  else
    bash "$SCRIPT_DIR/sync-opencode.sh"
  fi

  local src_mirror="$REPO_ROOT/.opencode/skills/skill-evolver"
  local dst
  if [[ "$SCOPE" == "global" ]]; then
    dst="$HOME/.config/opencode/skills/skill-evolver"
  else
    echo "✓ OpenCode skill installed at: $src_mirror (project-local)"
    echo "  Launch OpenCode from this directory; it auto-discovers .opencode/skills/"
    return
  fi
  clean_copy "$src_mirror" "$dst"
  echo "✓ OpenCode skill installed at: $dst"
}

# ─────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────

echo "skill-evolver unified installer"
echo "  platform: $PLATFORM"
echo "  scope:    $SCOPE"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "  mode:     DRY RUN (nothing will be written)"
fi

case "$PLATFORM" in
  claude)   install_claude ;;
  codex)    install_codex ;;
  opencode) install_opencode ;;
  all)
    install_claude
    install_codex
    install_opencode
    ;;
esac

echo ""
echo "Done."
echo "Reminder: skill-evolver requires skill-creator as a hard dependency."
echo "          If not installed, run /install skill-creator in Claude Code,"
echo "          or set SKILL_CREATOR_PATH. See README for details."
