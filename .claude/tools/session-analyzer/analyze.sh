#!/bin/bash
# Claude Session Analyzer - CLI wrapper
#
# Usage:
#   claude-session-analyzer                    # Analyze latest session in current project
#   claude-session-analyzer --latest           # Same as above
#   claude-session-analyzer <session-id>       # Analyze specific session
#   claude-session-analyzer <path/to.jsonl>    # Analyze a transcript by path
#   claude-session-analyzer --project /path    # Analyze sessions in specific project
#   claude-session-analyzer --output json      # Output JSON instead of HTML
#   claude-session-analyzer --open             # Open HTML in browser
#   claude-session-analyzer --list             # List all sessions with summaries
#   claude-session-analyzer --latest --path    # Print current session transcript path only
#   claude-session-analyzer <id> --overview    # Session topology: sub-agents, workflows, totals
#
# Examples:
#   claude-session-analyzer --latest --open
#   claude-session-analyzer bf5793b6 --open
#   claude-session-analyzer --output json | jq '.stats'
#   claude-session-analyzer --list

# Resolve symlinks to find actual script location
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

exec python3 "$SCRIPT_DIR/parser.py" "$@"
