# Claude Code + Codex — Local Observability Stack

A self-contained local OpenTelemetry stack shared by Claude Code and Codex.
Collector, Prometheus, and Grafana all run on this machine.

```
Claude Code ─┐
             ├─ OTLP ─▶ OpenTelemetry Collector ─▶ Prometheus ─▶ Grafana
Codex ───────┘          :4317 / :4318             :9090          :3000
```

## Three visibility tiers

| Tier | Claude Code | Codex | Purpose |
|---|---|---|---|
| Live | `/usage`, `/cost`, statusline | native context/usage UI | Current task at a glance |
| Offline | `claude-session-analyzer` | `codex-session-analyzer` | Inspect any saved task, agents, models, tokens, and estimated API-rate cost |
| Historical | `ai-telemetry up` → Grafana | same shared stack | Cross-task token, model, tool, latency, and agent trends |

The offline analyzers read existing local transcripts and need no collector. The
historical tier receives native OTLP exports; it is optional and asynchronous.

## Start and stop

All three command names operate the same stack:

```bash
ai-telemetry up
claude-telemetry status       # compatibility name
codex-telemetry logs
ai-telemetry down
```

Aliases: `ai-obs`, `cc-obs`, `codex-obs`.

## URLs and dashboards

| Service | URL | Notes |
|---|---|---|
| Grafana | http://localhost:3000 | Anonymous admin; two dashboards are provisioned |
| Prometheus | http://localhost:9090 | Inspect raw `claude_code_*` and `codex_*` series |
| OTLP gRPC | localhost:4317 | Collector receiver |
| OTLP HTTP | localhost:4318 | Collector receiver used by the bundled configs |
| Collector metrics | localhost:8889 | Prometheus scrape endpoint |

Grafana dashboards:

- **Claude Code — Tokens & Cost**: tokens, model mix, cost, active time, and edit decisions.
- **Codex — Tokens, Agents & Tools**: tokens by type/model, tool outcomes, turn latency, and sub-agent spawns.

## Claude Code export configuration

`.claude/settings.json` ships the required environment variables:

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
    "OTEL_METRIC_EXPORT_INTERVAL": "10000",
    "OTEL_LOGS_EXPORT_INTERVAL": "5000"
  }
}
```

See Claude Code's [monitoring usage documentation](https://code.claude.com/docs/en/monitoring-usage).

## Codex export configuration

`.codex/config.toml` ships this native Codex configuration:

```toml
[otel]
environment = "local"
log_user_prompt = false
exporter = { otlp-http = { endpoint = "http://localhost:4318/v1/logs", protocol = "binary" } }
metrics_exporter = { otlp-http = { endpoint = "http://localhost:4318/v1/metrics", protocol = "binary" } }
```

`deploy.sh` merges the managed model/OTEL keys into `~/.codex/config.toml`.
It does not replace the file, so Codex Desktop projects, plugins, MCP servers,
notifications, permissions, and other user/app state survive deployment. Raw
user prompts are not exported (`log_user_prompt = false`).

Codex's native metrics currently include `codex.turn.token_usage`,
`codex.tool.call`, `codex.turn.e2e_duration_ms`, `codex.thread.started`, and
`codex.multi_agent.spawn`, with model/origin/session metadata. See the official
[Codex advanced configuration documentation](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry).

## Failure behavior

When the stack is down, both clients' OTLP exporters have nothing to connect to.
Export happens asynchronously and does not block the coding task. Start the
stack when you want historical capture; offline transcript analysis continues
to work either way.

Prometheus counters re-baseline when the collector restarts, but Prometheus and
Grafana both use named volumes, so collected history and dashboard state survive
ordinary container recreation and `down`/`up` cycles (unless volumes are removed).

## Metric-name translation

The collector's Prometheus exporter turns dots into underscores and adds
counter/unit/histogram suffixes. Exact names can also shift between client or
collector versions. Both bundled dashboards therefore select series with
regexes, for example:

```promql
sum by (token_type) (
  increase({__name__=~"codex_turn_token_usage.*_sum"}[$__rate_interval])
)
```

If a panel is empty, open Prometheus, search for `codex_` or `claude_code_`, and
adjust the expression to the emitted suffix. Labels such as `model`, `tool`,
`success`, and `token_type` remain available because the collector enables
`resource_to_telemetry_conversion`.

## Files

```
.claude/observability/
  docker-compose.yml
  otel-collector-config.yaml
  prometheus.yml
  grafana/
    provisioning/
      datasources/datasource.yml
      dashboards/dashboards.yml
    dashboards/
      claude-code.json
      codex.json
  README.md
.codex/config.toml
.codex/tools/merge_config.py
.zsh/observability.zsh
```
