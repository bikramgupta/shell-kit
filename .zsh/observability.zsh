# observability.zsh - Claude Code local telemetry stack (OTel + Prometheus + Grafana)
# Run 'claude-telemetry help' for available commands

# ============================================================================
# HELPERS
# ============================================================================

# Resolve the observability docker-compose file. Prefer the deployed copy in
# ~/.claude, fall back to the shell-kit repo checkout so it works pre-deploy.
_claude_telemetry_compose_file() {
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

# ============================================================================
# HELP SYSTEM
# ============================================================================

_claude_telemetry_usage() {
  cat <<'EOF'
claude-telemetry - Local Claude Code observability stack
========================================================

Spins up an OpenTelemetry Collector + Prometheus + Grafana stack that
captures the metrics/logs Claude Code exports over OTLP. When the stack is
down, Claude Code's telemetry export just fails silently - no session impact.

USAGE:
  claude-telemetry <command>

COMMANDS:
  up         Start the stack in the background (docker compose up -d)
  down       Stop and remove the stack (docker compose down)
  restart    Restart the stack
  status     Show container status (docker compose ps)
  logs       Follow the otel-collector logs (raw exported events)
  help       Show this help (default when no command given)

URLS (once 'up'):
  Grafana      http://localhost:3000   (anonymous admin; dashboard: Claude Code — Tokens & Cost)
  Prometheus   http://localhost:9090
  OTLP gRPC    localhost:4317          (OTEL_EXPORTER_OTLP_ENDPOINT)
  OTLP HTTP    localhost:4318

Requires the OTEL_* env vars in ~/.claude/settings.json (see
.claude/observability/README.md). Alias: cc-obs
EOF
}

# ============================================================================
# MAIN COMMAND
# ============================================================================

claude-telemetry() {
  local cmd="${1:-help}"

  case "$cmd" in
    -h|--help|help|"")
      _claude_telemetry_usage
      return 0
      ;;
  esac

  if ! command -v docker >/dev/null 2>&1; then
    echo "claude-telemetry: docker is not installed or not on PATH" >&2
    return 1
  fi

  local compose_file
  compose_file="$(_claude_telemetry_compose_file)" || {
    echo "claude-telemetry: could not find observability/docker-compose.yml" >&2
    echo "  looked in ~/.claude/observability and the shell-kit repo checkout" >&2
    return 1
  }

  case "$cmd" in
    up)
      echo "Starting Claude Code telemetry stack..."
      docker compose -f "$compose_file" up -d || return 1
      echo ""
      echo "Grafana:    http://localhost:3000  (dashboard: Claude Code — Tokens & Cost)"
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
      echo "claude-telemetry: unknown command '$cmd'" >&2
      echo ""
      _claude_telemetry_usage
      return 1
      ;;
  esac
}

# Short alias
alias cc-obs='claude-telemetry'
