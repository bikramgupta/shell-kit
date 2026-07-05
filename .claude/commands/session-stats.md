---
description: Current session stats — transcript path, sub-agents, workflows, token/cost totals
allowed-tools: Bash(claude-session-analyzer:*), Bash(claude-workflow-analyzer:*), Bash(python3:*), Read
---

# Current Session Stats

Claude Code exports this session's id as `$CLAUDE_CODE_SESSION_ID`, so the transcript
is resolved **exactly** — robust even with multiple concurrent sessions in the same
project. (Falls back to `--latest` only if the env var is unset on an older build.)

## Context (gathered automatically)

Transcript path:

!`sid="${CLAUDE_CODE_SESSION_ID:-}"; if [ -n "$sid" ]; then claude-session-analyzer "$sid" --path 2>/dev/null; else claude-session-analyzer --latest --path 2>/dev/null; fi || echo "(no transcript found for this project)"`

Session topology — main agent, direct sub-agents, dynamic workflows, offloaded tool
results, and token/cost totals:

!`sid="${CLAUDE_CODE_SESSION_ID:-}"; if [ -n "$sid" ]; then claude-session-analyzer "$sid" --overview 2>/dev/null; else claude-session-analyzer --latest --overview 2>/dev/null; fi || echo "(analyzer unavailable — run ./deploy.sh from shell-kit, or check ~/.local/bin/claude-session-analyzer)"`

## Task

Present the results to the user concisely:

1. The **absolute transcript path** on its own line (easy to copy).
2. The **session id** (filename without `.jsonl`).
3. The **big picture**: how many direct sub-agents and dynamic workflow runs this
   session has spawned so far, with their paths, plus the session-total tokens and
   estimated cost. If there are none yet, say so in one line.
4. If workflows exist, mention that `claude-workflow-analyzer '<snapshot-path>'`
   (or `claude-workflow-analyzer <run-id>`) describes any run in detail — including
   from a plain shell outside Claude Code.

This is an at-a-glance reference. Do not open the HTML report or run further analysis
unless the user explicitly asks. The cost figure is a labeled **estimate**; `/cost`
and `/usage` are authoritative.
