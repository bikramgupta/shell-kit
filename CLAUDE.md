# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**shell-kit** - Modular zsh configuration + Claude Code settings for macOS. Deploys both shell tools and Claude Code configuration.

## Observability model (why this project exists)

The point of the tooling here is to answer two questions: **(1) am I learning and
improving over time**, and **(2) how many tokens/dollars did a given task and session
cost, broken down**. That drives a clean split:

- **Control plane = hooks.** Guard/inject only, silent and fail-open. The *only* hook is a
  SessionStart env-loader that sources project-local `.env`/`.env.local`. There are **no**
  event-logging hooks (they were noisy and never captured tokens). Safety guards live in
  `settings.json` → `permissions.deny`.
- **Observability plane = telemetry + transcripts**, in three tiers:
  - **Tier 1 (live):** native `/usage` and `/cost`, plus `statusline.sh` — the at-a-glance
    cost/context bar.
  - **Tier 2 (offline):** the session analyzers read each transcript's `usage` data and
    report per-session / per-agent / per-model **token + cost** in `--list`, `--digest`,
    and HTML. Cost is a labeled **estimate**; `/cost` and the Usage API are authoritative.
  - **Tier 3 (historical):** local OTEL Collector → Prometheus → Grafana stack in
    `.claude/observability/`, driven by `CLAUDE_CODE_ENABLE_TELEMETRY` in `settings.json`.
    Managed with `claude-telemetry up|down|status`. When the stack is down, OTLP export
    fails silently with no session impact.

See `docs/OBSERVABILITY.md` for the full rationale and usage guide.

## Deployment

```bash
./deploy.sh           # Interactive deployment with diff preview and backups
./deploy.sh --force   # Skip confirmations (still creates backups)
```

Creates timestamped backups:
- Zsh: `~/.zsh-backup/YYYYMMDD_HHMMSS/`
- Claude: `~/.claude-backup/YYYYMMDD_HHMMSS/`
- Codex: `~/.codex-backup/YYYYMMDD_HHMMSS/`

After deployment: `source ~/.zshrc`

## Architecture

```
zshrc                    # Loader + Homebrew + completions + starship + zsh plugins
.zsh/
  docker-tools.zsh       # Docker/Compose aliases (dkps, dc, dcu, etc.)
  extras.zsh             # PATH exports, NVM, FZF config, Python venv helpers
  git-tools.zsh          # Git/worktree power-user commands (gwt-*, gbr-*, etc.)
  hunt.zsh               # Unified search using fd/rg/fzf
  observability.zsh      # claude-telemetry (up/down/status) for the Grafana stack
.claude/
  CLAUDE.md              # Personal defaults for all projects
  settings.json          # Permissions, telemetry env, env-loader hook, statusline
  statusline.sh          # Custom status bar script (Tier 1 live cost/context bar)
  commands/              # Custom command definitions (e.g., squash-commits)
  tools/session-analyzer/ # Session analysis tool (Tier 2: tokens + cost estimate)
  observability/         # Tier 3: OTEL Collector + Prometheus + Grafana docker stack
.codex/
  config.toml            # Codex CLI settings (model, trust levels)
  tools/session-analyzer/ # Session analysis tool for Codex (tokens + cost estimate)
docs/
  OBSERVABILITY.md       # Observability vision + two-plane / three-tier guide
```

## Help Commands

- `ghelp` - Git/worktree command reference
- `dkhelp` - Docker command reference
- `hunt -h` - Search command help
- `claude-session-analyzer --help` - Claude session analysis tool
- `codex-session-analyzer --help` - Codex session analysis tool
- `claude-telemetry help` - Grafana/OTEL observability stack control (`cc-obs` alias)

Most functions also support `--help` flag:
- `gwt-ship --help`, `gwt-new --help`, `gwt-go --help`, `gwt-clone-bare --help`
- `dkexec --help`, `dcsh --help`

## Worktree Layout Convention

Git worktree commands (`gwt-*`) use a bare repository layout:
- `.bare/` directory at repo root contains the actual git data
- `.git` file (not directory) points to `.bare/`
- Worktrees are sibling directories named `{repo}-{branch}` (slashes become `__`)

Example: `gwt-ship myproject main` creates:
```
myproject/
  .bare/           # Bare git repo
  .git             # File containing "gitdir: ./.bare"
  myproject-main/  # Worktree for main branch
```

## Key Functions

**Repo initialization:**
- `gwt-ship <name> <branch>` - Create repo + GitHub remote + push (all-in-one)
- `gwt-clone-bare <url>` - Clone existing repo into bare structure

**Worktree ops:**
- `gwt-new <branch> [base]` - New branch + worktree
- `gwt-go <branch>` - cd to worktree
- `gwf` / `gwt-fzf` - Fuzzy find and switch worktree

**Search:**
- `hunt "*.py"` - Find files by name
- `hunt -c "pattern"` - Search file contents
- `hunt -i "*.tsx"` - Interactive mode with fzf preview

**Claude tools:**
- `claude-session-analyzer --list` - List all sessions
- `claude-session-analyzer --latest --open` - View latest session trace
- `claude-session-analyzer --latest --digest` - Markdown digest for session continuation

**Codex tools:**
- `codex-session-analyzer --list` - List all sessions
- `codex-session-analyzer --latest --open` - View latest session trace
- `codex-session-analyzer --latest --digest` - Markdown digest for session continuation

**Telemetry (Tier 3):**
- `claude-telemetry up` - Start the OTEL + Prometheus + Grafana stack (Grafana at :3000)
- `claude-telemetry status` - Show stack health
- `claude-telemetry down` - Stop the stack

## Dependencies

Required: `fd`, `rg` (ripgrep)
Optional: `fzf`, `bat`, `eza`, `starship`, `gh` (GitHub CLI for gwt-ship)
