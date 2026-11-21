#!/usr/bin/env bash
# ==============================================================================
# install.sh — One-Click Multi-Agent Skill Installer for Compute Steward
#
# Installs `compute-steward` into all major AI coding assistants on macOS/Linux/WSL:
#   - Universal Agent Skills (`~/.agents/skills/compute-steward`)
#   - Claude Code (`~/.claude/skills/compute-steward`)
#   - OpenAI Codex CLI (`~/.codex/skills/compute-steward`)
#   - Google Antigravity / Gemini CLI (`~/.gemini/config/skills/compute-steward`)
#   - Cursor (`~/.cursor/skills/compute-steward`)
#
# Usage:
#   ./install.sh              # Install via symlinks (updates automatically on git pull)
#   ./install.sh --copy       # Copy files instead of symlinking
#   ./install.sh --uninstall  # Remove from all agent skill directories
# ==============================================================================
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="compute-steward"
MODE="symlink"

for arg in "$@"; do
  case "$arg" in
    --copy) MODE="copy" ;;
    --uninstall) MODE="uninstall" ;;
  esac
done

chmod +x "$SRC_DIR/scripts/probe.py" "$SRC_DIR/scripts/safe_run.sh" "$SRC_DIR/scripts/task_watch.py" "$SRC_DIR/scripts/dashboard_server.py" 2>/dev/null || true

TARGET_DIRS=(
  "$HOME/.agents/skills"
  "$HOME/.claude/skills"
  "$HOME/.codex/skills"
  "$HOME/.gemini/config/skills"
  "$HOME/.cursor/skills"
)

echo "==> Compute Steward Skill Installer (mode: $MODE)"
for base in "${TARGET_DIRS[@]}"; do
  dest="$base/$SKILL_NAME"
  if [[ "$MODE" == "uninstall" ]]; then
    if [[ -L "$dest" || -d "$dest" ]]; then
      rm -rf "$dest"
      echo "  [Removed] $dest"
    fi
    continue
  fi

  mkdir -p "$base"
  rm -rf "$dest"
  if [[ "$MODE" == "copy" ]]; then
    mkdir -p "$dest"
    cp -R "$SRC_DIR/SKILL.md" "$SRC_DIR/README.md" "$SRC_DIR/LICENSE" "$SRC_DIR/scripts" "$SRC_DIR/references" "$dest/"
    rm -rf "$dest/scripts/__pycache__"
    echo "  [Copied]  $dest"
  else
    ln -s "$SRC_DIR" "$dest"
    echo "  [Linked]  $dest -> $SRC_DIR"
  fi
done

if [[ "$MODE" != "uninstall" ]]; then
  echo ""
  echo "✅ Successfully installed '$SKILL_NAME' to all AI Agent skill directories!"
  echo "   Try running a live hardware check right now:"
  echo "     python3 \"$SRC_DIR/scripts/probe.py\" --pretty"
  echo "   Or launch the live web dashboard:"
  echo "     python3 \"$SRC_DIR/scripts/dashboard_server.py\" --port 8765"
fi
