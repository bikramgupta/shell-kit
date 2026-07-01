# Claude Code — Local Observability Stack

A self-contained, local-only telemetry stack for Claude Code. Everything runs
in Docker on your machine; nothing leaves the host.

```
Claude Code  ──OTLP──▶  OpenTelemetry Collector  ──scrape──▶  Prometheus  ──▶  Grafana
 (:4317/:4318)              (re-exposes :8889)                   (:9090)         (:3000)
```

## What this is (Tier 3 of the observability model)

This repo has three tiers of visibility (full rationale in `docs/OBSERVABILITY.md`):

- **Tier 1 — Live:** native `/usage` and `/cost` plus `statusline.sh` — the
  at-a-glance cost/context bar for the session you're in right now.
- **Tier 2 — Offline per-session:** `claude-session-analyzer` reads each
  transcript's `usage` data and reports per-session / per-agent / per-model
  **token + cost** in `--list`, `--digest`, and HTML.
- **Tier 3 — Metrics & cost (this stack):** aggregate, time-series telemetry
  across *all* sessions — tokens, cost, active time, tool-decision rates —
  emitted by Claude Code's native OpenTelemetry support and visualized in
  Grafana. Tiers 1/2 answer "what happened in this session"; Tier 3 answers
  "what are my tokens/cost/behavior trends over time".

It's a **two-plane** design: the **control/UI plane** (Grafana + Prometheus you
look at) is fully decoupled from the **data plane** (the OTLP export from Claude
Code). If this stack is down, the data plane export simply fails silently — see
[Failure behavior](#failure-behavior).

## Start / stop

Use the `claude-telemetry` helper (from `.zsh/observability.zsh`, alias `cc-obs`):

```bash
claude-telemetry up       # start collector + prometheus + grafana (detached)
claude-telemetry status   # docker compose ps
claude-telemetry logs     # follow the collector (see raw exported events)
claude-telemetry down     # stop & remove
claude-telemetry restart
claude-telemetry help     # usage (also the no-arg default)
```

Or directly:

```bash
docker compose -f ~/.claude/observability/docker-compose.yml up -d
```

## URLs

| Service      | URL                     | Notes                                  |
|--------------|-------------------------|----------------------------------------|
| Grafana      | http://localhost:3000   | Anonymous admin; dashboard **Claude Code — Tokens & Cost** is auto-provisioned |
| Prometheus   | http://localhost:9090   | Query/verify raw series at `/graph`    |
| OTLP gRPC    | localhost:4317          | Claude Code's default OTLP endpoint    |
| OTLP HTTP    | localhost:4318          |                                        |
| Collector metrics | localhost:8889     | Prometheus scrape target               |

## Env vars Claude Code needs

Claude Code emits telemetry only when these env vars are set. They belong in the
`env` block of `~/.claude/settings.json` (that file is managed separately in this
repo, so add the block there — it is **not** wired up by these files):

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

This is exactly the block shipped in this repo's `.claude/settings.json`. It uses
the **HTTP/protobuf** OTLP endpoint (`:4318`); the collector also accepts gRPC on
`:4317` if you prefer `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` +
`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317`. (Equivalently, export them in
your shell.) The short export intervals make data show up in seconds during
testing; raise them (Claude's default is `60000` ms) for everyday use. Other
optional tuning: `OTEL_METRICS_INCLUDE_SESSION_ID` (default `true`). Source:
Claude Code [monitoring-usage docs](https://code.claude.com/docs/en/monitoring-usage).

## Failure behavior

When this stack is **down**, Claude Code's OTLP exporter has nothing to connect
to and the metric/log export **fails silently in the background** — it does not
block, slow, or error your Claude Code session. Start the stack whenever you want
data; there's no need to keep it running. (Prometheus counters reset when the
collector restarts, so cumulative stat panels re-baseline on each `up`.)

## Metric names & the dashboard caveat

> **Metric names can shift between Claude Code versions.** Always verify against
> the [monitoring-usage docs](https://code.claude.com/docs/en/monitoring-usage)
> and against the live series in Prometheus (`/graph`), and adjust the dashboard
> exprs if needed.

Claude Code emits these OTLP metrics (verified against the docs, 2026-07):

| OTLP metric                          | Unit   | Key attributes → Prometheus labels |
|--------------------------------------|--------|------------------------------------|
| `claude_code.session.count`          | count  | `start_type`, `session_id` |
| `claude_code.token.usage`            | tokens | `type` (input/output/cacheRead/cacheCreation), `model`, `query_source`, `session_id` |
| `claude_code.cost.usage`             | USD    | `model`, `query_source`, `session_id` |
| `claude_code.code_edit_tool.decision`| count  | `tool_name`, `decision` (accept/reject), `source`, `language` |
| `claude_code.active_time.total`      | s      | `type` (user/cli) |
| `claude_code.lines_of_code.count`    | count  | `type` (added/removed), `model` |
| `claude_code.commit.count` / `claude_code.pull_request.count` | count | standard attrs |

### OTEL → Prometheus name translation

The collector's `prometheus` exporter rewrites names: **dots become
underscores**, and (with metric suffixes on, the default) **counters get a
`_total` suffix** and the **unit is appended**. So the likely Prometheus names
are best-guesses:

| OTLP name                      | Best-guess Prometheus name |
|--------------------------------|----------------------------|
| `claude_code.token.usage`      | `claude_code_token_usage_tokens_total` |
| `claude_code.cost.usage`       | `claude_code_cost_usage_USD_total` |
| `claude_code.session.count`    | `claude_code_session_count_total` |
| `claude_code.code_edit_tool.decision` | `claude_code_code_edit_tool_decision_total` |
| `claude_code.active_time.total`| `claude_code_active_time_total_seconds_total` |
| `claude_code.lines_of_code.count` | `claude_code_lines_of_code_count_total` |

The docs do **not** pin down the exact suffixes, so to stay robust the bundled
dashboard **does not hard-code them** — every panel selects by regex, e.g.
`sum by (type) (increase({__name__=~"claude_code_token_usage.*"}[$__rate_interval]))`.
That matches whatever suffix your Claude Code / collector version produces. If a
panel is empty, open Prometheus `/graph`, type `claude_code` to autocomplete the
real series names, and update the exprs accordingly.

Dashboard attribute labels (`type`, `model`, `decision`, ...) come through
because the collector config enables `resource_to_telemetry_conversion` and
datapoint attributes are emitted as labels (dots in attribute names, e.g.
`session.id`, are sanitized to `session_id`).

## Files

```
.claude/observability/
  docker-compose.yml            # 3 services on the claude-obs network
  otel-collector-config.yaml    # OTLP in -> prometheus(:8889) + debug logs
  prometheus.yml                # scrape otel-collector:8889 @ 15s
  grafana/
    provisioning/
      datasources/datasource.yml  # Prometheus (uid: prometheus, default)
      dashboards/dashboards.yml   # loads /var/lib/grafana/dashboards
    dashboards/claude-code.json   # "Claude Code — Tokens & Cost" dashboard
  README.md                     # this file
.zsh/observability.zsh          # claude-telemetry / cc-obs helper
```
