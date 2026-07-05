# Claude Session Analyzer

A post-session analysis tool that parses Claude Code transcripts and generates interactive HTML visualizations with accurate agent attribution.

## Why This Exists

Claude Code hooks can't reliably identify which agent (main vs subagent) made a tool call because `session_id` is shared across all agents. This analyzer solves that by parsing the separate transcript files that Claude Code creates for each agent.

## Installation

```bash
# Create symlink for easy access
ln -s ~/.claude/tools/session-analyzer/analyze.sh ~/.local/bin/claude-session-analyzer
```

## Usage

```bash
# List all sessions with summaries
claude-session-analyzer --list

# Analyze latest session in current project
claude-session-analyzer --latest --open

# Analyze specific session (searches globally if not found locally)
claude-session-analyzer bf5793b6 --open

# Analyze by path — transcript .jsonl or per-session directory, any project
claude-session-analyzer ~/.claude/projects/<proj>/<session-id>.jsonl --overview

# Output JSON instead of HTML
claude-session-analyzer --output json

# Generate markdown digest for follow-up in new session
claude-session-analyzer 29108e9c --digest

# Save digest to file
claude-session-analyzer 29108e9c --digest > session-digest.md

# Analyze sessions in a specific project
claude-session-analyzer --project /path/to/project --list

# Print just the transcript path (fast, no parsing)
claude-session-analyzer <session-id> --path

# Session topology: sub-agents, dynamic workflows, offloaded tool results, totals
claude-session-analyzer <session-id> --overview
```

### Overview Output

The `--overview` flag prints the full on-disk topology of a session:

- **Main agent** - model, API request count, tokens, estimated cost
- **Direct sub-agents** - one line per Agent-tool sub-agent (type, tokens, cost,
  task description, transcript path)
- **Dynamic workflows** - one block per Workflow run (status, agent count,
  reported tokens, duration, snapshot/script/agents-dir paths)
- **Offloaded tool results** - large tool outputs stored outside the transcript
- **Session total** - main + sub-agents + workflow agents, per-model breakdown

Inside a session, the `/session-stats` command wraps this (pinned to
`$CLAUDE_CODE_SESSION_ID`, so it is correct with concurrent sessions).

## Workflow Analyzer (companion tool)

`workflow.py` (symlinked as `claude-workflow-analyzer`) describes dynamic
workflow runs from the shell, outside any session:

```bash
claude-workflow-analyzer                    # List all runs across all projects
claude-workflow-analyzer wf_9e75b43c-855    # Describe a run by id
claude-workflow-analyzer --latest --agents  # Newest run, with per-agent table
claude-workflow-analyzer --latest --samples # One sample agent per phase
```

### Digest Output

The `--digest` flag generates a concise markdown summary perfect for feeding into a new Claude session:

- **User Prompts** - Chronological list of what was asked
- **Key Thinking/Decisions** - Most substantive reasoning from Claude
- **Commands Run** - All bash commands executed
- **Files Modified** - Files that were edited or created
- **Errors Encountered** - Any tool errors that occurred
- **Where It Left Off** - Last prompt, response, and thinking

Use case: Session crashed or was force-closed? Generate a digest and paste it into a new session to continue where you left off.

## Features

### CLI
- `--list` - List all sessions with ID, duration, project, and summary
- `--latest` - Analyze most recent session
- `--digest` - Generate markdown digest for follow-up
- `--open` - Open HTML visualization in browser
- `--output json|html` - Choose output format
- `--project PATH` - Specify project directory
- Global fallback: If session not found locally, searches all projects

### HTML Visualization
- **Timeline View** - Chronological trace of all events
- **Tree View** - Hierarchical view grouped by agent
- **Event Types**:
  - User prompts (blue)
  - Thinking blocks (purple)
  - Text responses (green)
  - Tool calls (red) with inputs and results
  - Lifecycle events (purple) - Stop, SessionStart, SessionEnd, PreCompact
  - PostToolUse (green/red) - Success/error status for each tool call
- **Filters**:
  - By agent (orchestrator, Explore, Plan, etc.)
  - By event type
  - Text search
  - Checkbox toggles for each event type (including Lifecycle)
- **Session Selector** - Dropdown to switch between sessions
- **Stats** - Counts for agents, tools, user prompts, thinking, responses, lifecycle events

### Hook Log Integration

The analyzer automatically integrates events from hook logs (`.claude-logs/`) when available:

- **SessionStart/SessionEnd** - Session boundaries
- **Stop/SubagentStop** - Agent completion events
- **PreCompact** - Context compaction events
- **Notification** - System notifications
- **Permission** - User approval prompts
- **PostToolUse** - Success/error status for each tool call

Hook logs are discovered by matching timestamps with the session time window. If no hook logs exist, the analyzer works normally with just JSONL transcripts.

## How It Works

1. **Transcript Location**: `~/.claude/projects/{encoded-path}/{sessionId}.jsonl`
2. **Path Encoding**: Both `/` and `.` are replaced with `-`
3. **Per-session directory** (`{encoded-path}/{sessionId}/`, created on demand):

```
{sessionId}/
├── subagents/
│   ├── agent-{agentId}.jsonl        # Direct sub-agent transcripts (Agent tool)
│   ├── agent-{agentId}.meta.json    # {agentType, spawnDepth}
│   └── workflows/{runId}/           # Workflow-spawned agent transcripts
│       ├── agent-{agentId}.jsonl
│       └── journal.jsonl            # Workflow journal (started/result events)
├── workflows/
│   ├── wf_{runId}.json              # Workflow run snapshot (script, result, progress)
│   └── scripts/{name}-{runId}.js    # Persisted workflow scripts
└── tool-results/toolu_*.txt         # Large tool outputs offloaded from context
```

Older Claude Code versions wrote flat `agent-*.jsonl` files next to the main
transcript; both layouts are supported.

**Token accounting**: the main transcript's Task tool result records only the
`agentId` — token usage is never rolled up into the main file. Each sub-agent's
usage lives in its own transcript, which is why the analyzer aggregates across
all of them.

The analyzer:
1. Finds the main session transcript
2. Discovers all associated agent transcripts
3. Filters out warmup agents
4. Extracts full trace (user, thinking, text, tool_use, tool_result)
5. Detects agent types from prompts (Explore, Plan, claude-code-guide, etc.)
6. Builds unified timeline with accurate agent attribution
7. Generates interactive HTML or JSON output

## Files

```
~/.claude/tools/session-analyzer/
├── analyze.sh      # CLI entry point (handles symlinks)
├── parser.py       # Core parsing and HTML generation
├── workflow.py     # claude-workflow-analyzer: describe wf_*.json workflow runs
├── templates/      # (reserved for future templates)
└── README.md       # This file
```

## Dependencies

- Python 3.x (standard library only)
- Browser (for HTML visualization)
