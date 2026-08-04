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
  - **Tier 3 (historical):** shared local OTEL Collector → Prometheus → Grafana stack in
    `.claude/observability/`. Claude uses `CLAUDE_CODE_ENABLE_TELEMETRY`; Codex uses native
    `[otel]` config with raw prompts redacted. Managed with `ai-telemetry up|down|status`
    (`claude-telemetry` and `codex-telemetry` are compatibility names).

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
  observability.zsh      # ai/claude/codex-telemetry helpers for the shared Grafana stack
.claude/
  CLAUDE.md              # Personal defaults for all projects
  settings.json          # Permissions, telemetry env, env-loader hook, statusline
  statusline.sh          # Custom status bar script (Tier 1 live cost/context bar)
  commands/              # Custom command definitions (e.g., squash-commits)
  tools/session-analyzer/ # Session analysis tool (Tier 2: tokens + cost estimate)
                          #   narrative.py = turn-by-turn replay of a session
  observability/         # Tier 3: OTEL Collector + Prometheus + Grafana docker stack
.codex/
  config.toml            # Managed Codex model + native OTEL settings (merged on deploy)
  tools/merge_config.py  # Preserves app/plugin/MCP state while merging managed keys
  tools/session-analyzer/ # Root/sub-agent history, trace, token, model, and cost analysis
docs/
  OBSERVABILITY.md       # Observability vision + two-plane / three-tier guide
```

## Help Commands

- `ghelp` - Git/worktree command reference
- `dkhelp` - Docker command reference
- `hunt -h` - Search command help
- `claude-session-analyzer --help` - Claude session analysis tool
- `claude-workflow-analyzer --help` - Describe dynamic workflow runs (wf_*.json)
- `codex-session-analyzer --help` - Codex session analysis tool
- `ai-telemetry help` - Shared Grafana/OTEL control (`ai-obs`; provider-specific names work)

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
- `claude-session-analyzer <id> --overview` - Session topology: sub-agents, workflows, totals
- `claude-workflow-analyzer` - List/describe dynamic workflow runs (works outside sessions)
- `claude-workflow-analyzer <id> --mermaid` - Mermaid flowchart of a run (paste into markdown)
- `claude-workflow-analyzer <id> --diagram` - Self-contained HTML diagram (phase flow + timeline)
- `claude-session-narrative <id>` - Read a session as a story: prompt → tool calls → what
  each returned → reply. Flags failures, inferred retries, and oversized tool results.
  Opens a self-contained HTML page (no server); `--text` renders it in the terminal.
- `/session-stats` (in-session) - Current session's transcript path + topology overview

**Codex tools:**
- `codex-session-analyzer --list` - List all sessions
- `codex-session-analyzer --latest --open` - View latest session trace
- `codex-session-analyzer --latest --digest` - Markdown digest for session continuation
- `codex-session-analyzer <id> --overview` - Root/sub-agent topology and token/model totals
- `codex-session-analyzer <id> --path` - Resolve a transcript path
- `codex-session-analyzer --project /path --list` - Filter history to an exact project cwd

**Telemetry (Tier 3):**
- `ai-telemetry up` - Start shared OTEL + Prometheus + Grafana (Grafana at :3000)
- `codex-telemetry status` - Show the same shared stack's health
- `claude-telemetry down` - Stop the same shared stack

## Dependencies

Required: `fd`, `rg` (ripgrep)
Optional: `fzf`, `bat`, `eza`, `starship`, `gh` (GitHub CLI for gwt-ship)
