#!/bin/bash
# deploy.sh - Deploy shell-kit configuration to home directory
# Usage: ./deploy.sh [--force]
#
# This script:
# 1. Checks for additions in remote (~/) not present in local
# 2. Shows diffs between local and remote files
# 3. Creates timestamped backups before overwriting
# 4. Copies all configuration files (zsh + claude)

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Dependencies required for full shell experience
BREW_DEPS=(starship zsh-autosuggestions zsh-syntax-highlighting fd ripgrep)

# Paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_ZSHRC="$HOME/.zshrc"
HOME_ZSH_DIR="$HOME/.zsh"
LOCAL_ZSHRC="$SCRIPT_DIR/zshrc"
LOCAL_ZSH_DIR="$SCRIPT_DIR/.zsh"
BACKUP_DIR="$HOME/.zsh-backup/$(date +%Y%m%d_%H%M%S)"

FORCE=0
if [[ "$1" == "--force" || "$1" == "-f" ]]; then
  FORCE=1
fi

echo -e "${BLUE}=== Zshrc Deployment Script ===${NC}"
echo ""

# ============================================================================
# CHECK FOR ADDITIONS IN REMOTE NOT IN LOCAL
# ============================================================================

check_remote_additions() {
  local has_additions=0

  echo -e "${YELLOW}Checking for additions in ~/ not in local...${NC}"

  # Check ~/.zshrc for unique content
  if [[ -f "$HOME_ZSHRC" ]]; then
    # Extract non-comment, non-empty lines and compare
    local remote_lines
    remote_lines=$(grep -v '^[[:space:]]*#' "$HOME_ZSHRC" 2>/dev/null | grep -v '^[[:space:]]*$' | sort -u)
    local local_lines
    local_lines=$(grep -v '^[[:space:]]*#' "$LOCAL_ZSHRC" 2>/dev/null | grep -v '^[[:space:]]*$' | sort -u)

    # Find lines in remote not in local
    local unique_remote
    unique_remote=$(comm -23 <(echo "$remote_lines") <(echo "$local_lines") 2>/dev/null || true)

    if [[ -n "$unique_remote" ]]; then
      echo -e "${RED}WARNING: ~/.zshrc has content not in local:${NC}"
      echo "$unique_remote" | head -20
      if [[ $(echo "$unique_remote" | wc -l) -gt 20 ]]; then
        echo "... (more lines not shown)"
      fi
      echo ""
      has_additions=1
    fi
  fi

  # Check for .zsh files in home that don't exist locally
  if [[ -d "$HOME_ZSH_DIR" ]]; then
    for remote_file in "$HOME_ZSH_DIR"/*.zsh; do
      [[ -f "$remote_file" ]] || continue
      local filename
      filename=$(basename "$remote_file")
      local local_file="$LOCAL_ZSH_DIR/$filename"

      if [[ ! -f "$local_file" ]]; then
        echo -e "${RED}WARNING: ~/.zsh/$filename exists remotely but not locally!${NC}"
        has_additions=1
      fi
    done
  fi

  return $has_additions
}

# ============================================================================
# SHOW DIFFS
# ============================================================================

show_diffs() {
  echo -e "${YELLOW}Checking for differences...${NC}"
  echo ""

  local has_diff=0

  # Check .zshrc
  if [[ -f "$HOME_ZSHRC" ]]; then
    if ! diff -q "$LOCAL_ZSHRC" "$HOME_ZSHRC" >/dev/null 2>&1; then
      echo -e "${BLUE}--- ~/.zshrc differences ---${NC}"
      diff --color=auto -u "$HOME_ZSHRC" "$LOCAL_ZSHRC" 2>/dev/null | head -50 || true
      echo ""
      has_diff=1
    fi
  else
    echo -e "${GREEN}~/.zshrc does not exist (will create)${NC}"
    has_diff=1
  fi

  # Check .zsh directory files
  for local_file in "$LOCAL_ZSH_DIR"/*.zsh; do
    [[ -f "$local_file" ]] || continue
    local filename
    filename=$(basename "$local_file")
    local remote_file="$HOME_ZSH_DIR/$filename"

    if [[ -f "$remote_file" ]]; then
      if ! diff -q "$local_file" "$remote_file" >/dev/null 2>&1; then
        echo -e "${BLUE}--- ~/.zsh/$filename differences ---${NC}"
        diff --color=auto -u "$remote_file" "$local_file" 2>/dev/null | head -30 || true
        echo ""
        has_diff=1
      fi
    else
      echo -e "${GREEN}~/.zsh/$filename does not exist (will create)${NC}"
      has_diff=1
    fi
  done

  # Return 0 (success) if differences found, 1 (failure) if none
  [[ $has_diff -eq 1 ]] && return 0 || return 1
}

# ============================================================================
# CREATE BACKUPS
# ============================================================================

create_backups() {
  echo -e "${YELLOW}Creating backups in $BACKUP_DIR${NC}"
  mkdir -p "$BACKUP_DIR"

  if [[ -f "$HOME_ZSHRC" ]]; then
    cp "$HOME_ZSHRC" "$BACKUP_DIR/zshrc"
    echo "  Backed up: ~/.zshrc"
  fi

  if [[ -d "$HOME_ZSH_DIR" ]]; then
    cp -r "$HOME_ZSH_DIR" "$BACKUP_DIR/.zsh"
    echo "  Backed up: ~/.zsh/"
  fi

  echo ""
}

# ============================================================================
# DEPLOY FILES
# ============================================================================

deploy_files() {
  echo -e "${YELLOW}Deploying configuration files...${NC}"

  # Create ~/.zsh directory
  mkdir -p "$HOME_ZSH_DIR"

  # Copy .zshrc
  cp "$LOCAL_ZSHRC" "$HOME_ZSHRC"
  echo -e "  ${GREEN}Deployed:${NC} ~/.zshrc"

  # Copy all .zsh files
  for local_file in "$LOCAL_ZSH_DIR"/*.zsh; do
    [[ -f "$local_file" ]] || continue
    local filename
    filename=$(basename "$local_file")
    cp "$local_file" "$HOME_ZSH_DIR/$filename"
    echo -e "  ${GREEN}Deployed:${NC} ~/.zsh/$filename"
  done

  echo ""
}

# ============================================================================
# HOMEBREW DEPENDENCIES
# ============================================================================

install_dependencies() {
  # Check if Homebrew is available
  if ! command -v brew >/dev/null 2>&1; then
    echo -e "${RED}Homebrew not found. Please install Homebrew first:${NC}"
    echo "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    return 1
  fi

  echo -e "${YELLOW}Checking Homebrew dependencies...${NC}"

  local to_install=()
  local to_upgrade=()

  for pkg in "${BREW_DEPS[@]}"; do
    if brew list "$pkg" &>/dev/null; then
      # Package installed - check if outdated
      if brew outdated --quiet "$pkg" 2>/dev/null | grep -q "^$pkg$"; then
        to_upgrade+=("$pkg")
        echo -e "  ${YELLOW}↑${NC} $pkg (outdated)"
      else
        echo -e "  ${GREEN}✓${NC} $pkg (installed)"
      fi
    else
      to_install+=("$pkg")
      echo -e "  ${RED}✗${NC} $pkg (missing)"
    fi
  done

  # Install missing packages
  if [[ ${#to_install[@]} -gt 0 ]]; then
    echo -e "${YELLOW}Installing: ${to_install[*]}${NC}"
    brew install "${to_install[@]}"
  fi

  # Upgrade outdated packages
  if [[ ${#to_upgrade[@]} -gt 0 ]]; then
    echo -e "${YELLOW}Upgrading: ${to_upgrade[*]}${NC}"
    brew upgrade "${to_upgrade[@]}"
  fi

  if [[ ${#to_install[@]} -eq 0 ]] && [[ ${#to_upgrade[@]} -eq 0 ]]; then
    echo -e "${GREEN}All dependencies up to date.${NC}"
  fi

  echo ""
}

# ============================================================================
# CLAUDE SETTINGS DEPLOYMENT
# ============================================================================

LOCAL_CLAUDE_DIR="$SCRIPT_DIR/.claude"
HOME_CLAUDE_DIR="$HOME/.claude"
CLAUDE_BACKUP_DIR="$HOME/.claude-backup/$(date +%Y%m%d_%H%M%S)"

# Model prices are shared data, not adapter code. Copy the loader + the price
# table next to each analyzer so an installed tool is self-contained and does
# not reach back into this repo. Both analyzers look for these as siblings
# first, then fall back to shared/pricing/ when run from a checkout.
#
# Only these two files are copied. A user's own rates live in
# ~/.config/ai-tools/pricing.json, which deploy never writes, so re-running
# deploy.sh can never clobber prices you added.
deploy_pricing() {
  local dest="$1"
  local src="$SCRIPT_DIR/shared/pricing"
  [[ -d "$src" && -d "$dest" ]] || return 0
  cp "$src/pricing.py" "$src/models.json" "$dest/" 2>/dev/null || true
}

backup_claude_settings() {
  # Only backup if there are existing settings
  if [[ -f "$HOME_CLAUDE_DIR/settings.json" ]] || [[ -f "$HOME_CLAUDE_DIR/CLAUDE.md" ]]; then
    echo -e "${YELLOW}Backing up existing Claude settings...${NC}"
    mkdir -p "$CLAUDE_BACKUP_DIR"

    # Backup core config files only (not transient data)
    for f in CLAUDE.md settings.json statusline.sh; do
      [[ -f "$HOME_CLAUDE_DIR/$f" ]] && cp "$HOME_CLAUDE_DIR/$f" "$CLAUDE_BACKUP_DIR/"
    done
    [[ -d "$HOME_CLAUDE_DIR/hooks" ]] && cp -r "$HOME_CLAUDE_DIR/hooks" "$CLAUDE_BACKUP_DIR/"
    [[ -d "$HOME_CLAUDE_DIR/commands" ]] && cp -r "$HOME_CLAUDE_DIR/commands" "$CLAUDE_BACKUP_DIR/"
    [[ -d "$HOME_CLAUDE_DIR/agents" ]] && cp -r "$HOME_CLAUDE_DIR/agents" "$CLAUDE_BACKUP_DIR/"
    [[ -d "$HOME_CLAUDE_DIR/tools" ]] && cp -r "$HOME_CLAUDE_DIR/tools" "$CLAUDE_BACKUP_DIR/"

    echo -e "  ${GREEN}Backup saved to:${NC} $CLAUDE_BACKUP_DIR"
    echo ""
  fi
}

deploy_claude_settings() {
  [[ -d "$LOCAL_CLAUDE_DIR" ]] || return 0

  # Backup existing settings first
  backup_claude_settings

  echo -e "${YELLOW}Deploying Claude settings...${NC}"

  mkdir -p "$HOME_CLAUDE_DIR"/{commands,agents,tools,observability}

  # Core files
  for f in CLAUDE.md settings.json statusline.sh; do
    if [[ -f "$LOCAL_CLAUDE_DIR/$f" ]]; then
      cp "$LOCAL_CLAUDE_DIR/$f" "$HOME_CLAUDE_DIR/"
      echo -e "  ${GREEN}Deployed:${NC} ~/.claude/$f"
    fi
  done

  # Remove legacy event-logging hooks (superseded by transcripts + OTEL telemetry)
  if [[ -d "$HOME_CLAUDE_DIR/hooks" ]]; then
    rm -f "$HOME_CLAUDE_DIR/hooks/log-tool-calls.sh" "$HOME_CLAUDE_DIR/hooks/log-session-start.sh"
    rmdir "$HOME_CLAUDE_DIR/hooks" 2>/dev/null || true
    echo -e "  ${YELLOW}Removed:${NC} legacy ~/.claude/hooks logging scripts"
  fi

  # Observability stack (OTEL collector + Prometheus + Grafana; Tier 3)
  if [[ -d "$LOCAL_CLAUDE_DIR/observability" ]]; then
    cp -r "$LOCAL_CLAUDE_DIR/observability/"* "$HOME_CLAUDE_DIR/observability/" 2>/dev/null || true
    echo -e "  ${GREEN}Deployed:${NC} ~/.claude/observability/"

    # Tier 3 dashboards need Docker. Never fail the deploy over it: telemetry stays
    # enabled system-wide in settings.json, and Claude Code's OTLP export silently
    # no-ops when nothing is listening. Just warn so the user knows what to do.
    if ! command -v docker >/dev/null 2>&1; then
      echo -e "  ${YELLOW}WARNING:${NC} Docker not found — Tier 3 telemetry dashboards are bypassed."
      echo -e "           Claude Code's OTLP export fails silently (no session impact)."
      echo -e "           Install Docker, then run 'ai-telemetry up' to enable Grafana."
    elif ! docker info >/dev/null 2>&1; then
      echo -e "  ${YELLOW}Telemetry:${NC} Docker installed but not running — start Docker, then 'ai-telemetry up'."
    else
      echo -e "  ${GREEN}Telemetry:${NC} Docker detected — run 'ai-telemetry up' for Grafana dashboards."
    fi
  fi

  # Commands
  if [[ -d "$LOCAL_CLAUDE_DIR/commands" ]]; then
    for f in "$LOCAL_CLAUDE_DIR/commands"/*; do
      [[ -f "$f" ]] && cp "$f" "$HOME_CLAUDE_DIR/commands/"
    done
    echo -e "  ${GREEN}Deployed:${NC} ~/.claude/commands/"
  fi

  # Agents
  if [[ -d "$LOCAL_CLAUDE_DIR/agents" ]]; then
    mkdir -p "$HOME_CLAUDE_DIR/agents"
    for f in "$LOCAL_CLAUDE_DIR/agents"/*; do
      [[ -f "$f" ]] && cp "$f" "$HOME_CLAUDE_DIR/agents/"
    done
    echo -e "  ${GREEN}Deployed:${NC} ~/.claude/agents/"
  fi

  # Tools (recursive copy)
  if [[ -d "$LOCAL_CLAUDE_DIR/tools" ]]; then
    cp -r "$LOCAL_CLAUDE_DIR/tools/"* "$HOME_CLAUDE_DIR/tools/" 2>/dev/null || true
    # Ensure scripts are executable
    chmod +x "$HOME_CLAUDE_DIR/tools/session-analyzer/analyze.sh" 2>/dev/null || true
    chmod +x "$HOME_CLAUDE_DIR/tools/session-analyzer/parser.py" 2>/dev/null || true
    chmod +x "$HOME_CLAUDE_DIR/tools/session-analyzer/workflow.py" 2>/dev/null || true
    chmod +x "$HOME_CLAUDE_DIR/tools/session-analyzer/narrative.py" 2>/dev/null || true
    deploy_pricing "$HOME_CLAUDE_DIR/tools/session-analyzer"
    echo -e "  ${GREEN}Deployed:${NC} ~/.claude/tools/"
  fi

  # Create symlinks for session-analyzer + workflow-analyzer CLIs
  mkdir -p "$HOME/.local/bin"
  local analyzer="$HOME_CLAUDE_DIR/tools/session-analyzer/analyze.sh"
  if [[ -f "$analyzer" ]]; then
    ln -sf "$analyzer" "$HOME/.local/bin/claude-session-analyzer"
    echo -e "  ${GREEN}Symlinked:${NC} claude-session-analyzer -> ~/.local/bin/"
  fi
  local wf_analyzer="$HOME_CLAUDE_DIR/tools/session-analyzer/workflow.py"
  if [[ -f "$wf_analyzer" ]]; then
    ln -sf "$wf_analyzer" "$HOME/.local/bin/claude-workflow-analyzer"
    echo -e "  ${GREEN}Symlinked:${NC} claude-workflow-analyzer -> ~/.local/bin/"
  fi
  local narrative="$HOME_CLAUDE_DIR/tools/session-analyzer/narrative.py"
  if [[ -f "$narrative" ]]; then
    ln -sf "$narrative" "$HOME/.local/bin/claude-session-narrative"
    echo -e "  ${GREEN}Symlinked:${NC} claude-session-narrative -> ~/.local/bin/"
  fi

  echo ""
}

# ============================================================================
# CODEX SETTINGS DEPLOYMENT
# ============================================================================

LOCAL_CODEX_DIR="$SCRIPT_DIR/.codex"
HOME_CODEX_DIR="$HOME/.codex"
CODEX_BACKUP_DIR="$HOME/.codex-backup/$(date +%Y%m%d_%H%M%S)"

backup_codex_settings() {
  if [[ -f "$HOME_CODEX_DIR/config.toml" ]]; then
    echo -e "${YELLOW}Backing up existing Codex settings...${NC}"
    mkdir -p "$CODEX_BACKUP_DIR"
    [[ -f "$HOME_CODEX_DIR/config.toml" ]] && cp "$HOME_CODEX_DIR/config.toml" "$CODEX_BACKUP_DIR/"
    [[ -d "$HOME_CODEX_DIR/tools" ]] && cp -r "$HOME_CODEX_DIR/tools" "$CODEX_BACKUP_DIR/"
    echo -e "  ${GREEN}Backup saved to:${NC} $CODEX_BACKUP_DIR"
    echo ""
  fi
}

deploy_codex_settings() {
  [[ -d "$LOCAL_CODEX_DIR" ]] || return 0

  backup_codex_settings

  echo -e "${YELLOW}Deploying Codex settings...${NC}"

  mkdir -p "$HOME_CODEX_DIR/tools"

  # Merge shell-kit-managed model + OTEL settings into the live config. Codex
  # Desktop and plugins also maintain this file (projects, MCP servers, plugin
  # state, notification helpers), so replacing it wholesale destroys live state.
  if [[ -f "$LOCAL_CODEX_DIR/config.toml" ]]; then
    python3 "$LOCAL_CODEX_DIR/tools/merge_config.py" \
      "$LOCAL_CODEX_DIR/config.toml" "$HOME_CODEX_DIR/config.toml"
    echo -e "  ${GREEN}Merged:${NC} ~/.codex/config.toml (model + OTEL; preserved app/plugin state)"
  fi

  # Tools (recursive copy)
  if [[ -d "$LOCAL_CODEX_DIR/tools" ]]; then
    cp -r "$LOCAL_CODEX_DIR/tools/"* "$HOME_CODEX_DIR/tools/" 2>/dev/null || true
    chmod +x "$HOME_CODEX_DIR/tools/session-analyzer/analyze.sh" 2>/dev/null || true
    chmod +x "$HOME_CODEX_DIR/tools/session-analyzer/parser.py" 2>/dev/null || true
    chmod +x "$HOME_CODEX_DIR/tools/session-analyzer/narrative.py" 2>/dev/null || true
    chmod +x "$HOME_CODEX_DIR/tools/merge_config.py" 2>/dev/null || true
    deploy_pricing "$HOME_CODEX_DIR/tools/session-analyzer"
    echo -e "  ${GREEN}Deployed:${NC} ~/.codex/tools/"
  fi

  # Symlinks for session-analyzer + narrative CLIs
  mkdir -p "$HOME/.local/bin"
  local analyzer="$HOME_CODEX_DIR/tools/session-analyzer/analyze.sh"
  if [[ -f "$analyzer" ]]; then
    ln -sf "$analyzer" "$HOME/.local/bin/codex-session-analyzer"
    echo -e "  ${GREEN}Symlinked:${NC} codex-session-analyzer -> ~/.local/bin/"
  fi
  local codex_narrative="$HOME_CODEX_DIR/tools/session-analyzer/narrative.py"
  if [[ -f "$codex_narrative" ]]; then
    ln -sf "$codex_narrative" "$HOME/.local/bin/codex-session-narrative"
    echo -e "  ${GREEN}Symlinked:${NC} codex-session-narrative -> ~/.local/bin/"
  fi

  echo ""
}

# ============================================================================
# MAIN
# ============================================================================

# Check local files exist
if [[ ! -f "$LOCAL_ZSHRC" ]]; then
  echo -e "${RED}Error: $LOCAL_ZSHRC not found${NC}"
  exit 1
fi

if [[ ! -d "$LOCAL_ZSH_DIR" ]]; then
  echo -e "${RED}Error: $LOCAL_ZSH_DIR not found${NC}"
  exit 1
fi

echo "Local source:  $SCRIPT_DIR"
echo "Deploy target: $HOME"
echo ""

# Install/upgrade Homebrew dependencies
install_dependencies

# Check for remote additions
if ! check_remote_additions; then
  if [[ $FORCE -eq 0 ]]; then
    echo -e "${RED}Remote has additions not in local repository.${NC}"
    echo "Please review and add them to local first, or use --force to override."
    exit 1
  else
    echo -e "${YELLOW}--force specified, continuing despite remote additions...${NC}"
  fi
else
  echo -e "${GREEN}No remote additions detected.${NC}"
fi
echo ""

# Show diffs (capture result without triggering set -e)
if show_diffs; then
  has_zsh_diff=0
else
  has_zsh_diff=1
fi

# Confirm deployment
if [[ $FORCE -eq 0 ]]; then
  echo -e "${YELLOW}Ready to deploy. This will overwrite existing files.${NC}"
  read -p "Continue? [y/N] " -n 1 -r
  echo ""
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
  fi
fi

# Create backups and deploy
if [[ $has_zsh_diff -eq 0 ]]; then
  create_backups
  deploy_files
fi
deploy_claude_settings
deploy_codex_settings

echo -e "${GREEN}=== Deployment complete! ===${NC}"
echo ""
echo "Backups saved to:"
echo "  Zsh:    $BACKUP_DIR"
[[ -d "$CLAUDE_BACKUP_DIR" ]] && echo "  Claude: $CLAUDE_BACKUP_DIR"
[[ -d "$CODEX_BACKUP_DIR" ]] && echo "  Codex:  $CODEX_BACKUP_DIR"
echo ""
echo "To activate, run:"
echo "  source ~/.zshrc"
echo ""
echo "Quick help:"
echo "  ghelp                    - Git commands"
echo "  dkhelp                   - Docker commands"
echo "  hunt -h                  - Search commands"
echo "  claude-session-analyzer  - Analyze Claude sessions (tokens + cost)"
echo "  claude-workflow-analyzer - Describe dynamic workflow runs (wf_*.json)"
echo "  claude-session-narrative - Read a session turn by turn (prompt -> tools -> reply)"
echo "  codex-session-analyzer   - Analyze Codex sessions (tokens + cost)"
echo "  codex-session-narrative  - Read a Codex session turn by turn (prompt -> tools -> reply)"
echo "  ai-telemetry help        - Shared Claude + Codex OTEL/Grafana stack (ai-obs)"
