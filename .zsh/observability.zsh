# observability.zsh - Shared Claude Code + Codex local OTEL stack
# Run 'ai-telemetry help', 'claude-telemetry help', or 'codex-telemetry help'.

_ai_telemetry_compose_file() {
  local candidates=(
    "$HOME/.claude/observability/docker-compose.yml"
    "$HOME/Documents/Build/shell-kit/.claude/observability/docker-compose.yml"
  )
  local f
  for f in "${candidates[@]}"; do
    [[ -f "$f" ]] && { printf '%s\n' "$f"; return 0; }
  done
  return 1
}

_ai_telemetry_usage() {
  cat <<'EOF'
ai-telemetry - Local Claude Code + Codex observability stack
============================================================

Runs one OpenTelemetry Collector + Prometheus + Grafana stack for both
assistants. Claude Code exports via environment variables; Codex exports via
the [otel] block merged into ~/.codex/config.toml by shell-kit's deploy.sh.
Raw Codex user prompts remain redacted.

USAGE:
  ai-telemetry <command>
  claude-telemetry <command>   # compatibility wrapper
  codex-telemetry <command>    # same shared stack

COMMANDS:
  up         Start the stack in the background (docker compose up -d)
  down       Stop and remove the stack
  restart    Restart the stack
  status     Show container status
  logs       Follow collector logs from both assistants
  help       Show this help (default)

URLS:
  Grafana      http://localhost:3000
    - Claude Code — Tokens & Cost
    - Codex — Tokens, Agents & Tools
  Prometheus   http://localhost:9090
  OTLP gRPC    localhost:4317
  OTLP HTTP    localhost:4318

When the stack is down, both exporters fail asynchronously without blocking a
session. Aliases: ai-obs, cc-obs, codex-obs
EOF
}

_ai_telemetry() {
  local cmd="${1:-help}"

  case "$cmd" in
    -h|--help|help|"")
      _ai_telemetry_usage
      return 0
      ;;
  esac

  if ! command -v docker >/dev/null 2>&1; then
    echo "ai-telemetry: docker is not installed or not on PATH" >&2
    return 1
  fi

  local compose_file
  compose_file="$(_ai_telemetry_compose_file)" || {
    echo "ai-telemetry: could not find observability/docker-compose.yml" >&2
    echo "  looked in ~/.claude/observability and the shell-kit repo checkout" >&2
    return 1
  }

  case "$cmd" in
    up)
      echo "Starting shared Claude Code + Codex telemetry stack..."
      docker compose -f "$compose_file" up -d || return 1
      echo ""
      echo "Grafana:    http://localhost:3000"
      echo "Prometheus: http://localhost:9090"
      ;;
    down)
      docker compose -f "$compose_file" down
      ;;
    restart)
      docker compose -f "$compose_file" restart
      ;;
    status|ps)
      docker compose -f "$compose_file" ps
      ;;
    logs)
      docker compose -f "$compose_file" logs -f otel-collector
      ;;
    *)
      echo "ai-telemetry: unknown command '$cmd'" >&2
      echo ""
      _ai_telemetry_usage
      return 1
      ;;
  esac
}

ai-telemetry() { _ai_telemetry "$@"; }
claude-telemetry() { _ai_telemetry "$@"; }
codex-telemetry() { _ai_telemetry "$@"; }

alias ai-obs='ai-telemetry'
alias cc-obs='claude-telemetry'
alias codex-obs='codex-telemetry'
