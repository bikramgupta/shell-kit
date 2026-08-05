# Observability

## Why this exists

Every log line, hook, and dashboard in shell-kit should earn its place by answering one of
two questions. If it answers neither, it's noise and it gets removed.

1. **Am I learning and improving?** Over weeks, is my cost-per-task trending down? Am I
   leaning on the right models? Are my tool edits getting accepted or thrown away?
2. **What did this task and session cost — broken down?** For a given piece of work, how many
   tokens went in and out, split by model and by sub-agent, and roughly how many dollars?

The old setup answered *neither*. It logged tool **events** to text files via hooks — which
told you *what happened* but never *what it cost* (no tokens, no dollars anywhere) — and it
did so with a fragile `set -e` script that failed on every Bash call, printing
`PreToolUse:Bash hook error — Failed with non-blocking status code` into every session. It
was cost we paid (noise, fragility) for value we never got (token accounting).

This document is the rationale for the replacement.

## Two planes

The single most important idea here is that **hooks and observability are different jobs**
and must not be conflated.

### Control plane — hooks

Hooks exist to **guard and inject**, nothing else. They run in the critical path of every
tool call, so they must be **silent** and **fail-open**: a hook that errors, blocks, or
prints noise is worse than no hook at all. shell-kit keeps exactly one:

- **SessionStart env-loader** — sources project-local `.env` and `.env.local` from the
  current git root (or cwd) so each project gets its own environment. It ends in `; true`
  so it can never fail the session. It reads nothing from `$HOME` — ten projects, ten
  independent env files.

Safety guards that used to be imagined as "hook logic" live where they belong: in
`settings.json` → `permissions.deny` (`Read(~/.ssh/**)`, `Read(~/.aws/**)`, `Read(~/.do-*)`,
`Bash(rm -rf /)`, `Bash(rm -rf ~)`). Declarative, auditable, and they can't crash.

There are **no event-logging hooks.** Transcripts already record what happened, in more
detail than a hook could, so logging hooks were pure downside.

### Observability plane — telemetry + transcripts

This is where token/cost accounting lives, in three tiers that escalate from zero-setup to
durable history. Use the lowest tier that answers your question.

## Three tiers

| Tier | What | Setup | Answers | Authoritative? |
|------|------|-------|---------|----------------|
| **1 — Live** | native client usage/context UI; Claude `/usage`, `/cost`, statusline | none | current task, right now | Provider-native |
| **2 — Offline** | `claude-session-analyzer` / `codex-session-analyzer` (+ the matching `-narrative` tools) | none (reads local transcripts) | per-session / per-agent / per-model token + cost for any past session, and a turn-by-turn replay of what happened inside one | Estimate |
| **3 — Historical** | shared OTEL → Prometheus → Grafana stack | `ai-telemetry up` (Docker) | token/model/tool/agent trends across *all* tasks | Native exported metrics |

### Tier 1 — Live (native)

Nothing to install. Use the client-native context/usage display. In Claude Code:

- **`/usage`** — session tokens + cost + a breakdown of what's driving your limits
  (sub-agents, cache, long-context). This is the "insights" view.
- **`/cost`** — authoritative session cost.
- **`statusline.sh`** — the persistent bottom bar showing live cost and context usage. Kept
  as-is; it's the at-a-glance instrument.

Reach for Tier 1 when the question is *"what is this session costing me right now?"*

### Tier 2 — Offline per-session deep dive

The session analyzers parse the transcript JSONL you already have on disk and compute
**token + cost** per session, per agent (main vs each sub-agent), and per model.

```bash
claude-session-analyzer --list                 # all sessions, with Tokens + Est.$ columns
claude-session-analyzer --latest --digest      # markdown digest incl. a Token & Cost section
claude-session-analyzer --latest --open        # full HTML trace with token/cost stat tiles
codex-session-analyzer  --latest --digest      # same, for Codex (at parity)
codex-session-analyzer  --overview             # root + linked sub-agents + model totals
codex-session-analyzer  --open                 # combined agent-filterable HTML trace
codex-session-analyzer  --project /path --list # exact cwd history filter
```

Both providers also have a **narrative** view — the same session read as a story rather
than an event list, which is the view that answers *"where did the time and tokens
actually go?"*:

```bash
claude-session-narrative --latest              # prompt → tools → what returned → reply
codex-session-narrative  --latest              # same, for Codex
codex-session-narrative  <id> --text           # terminal rendering instead of HTML
```

Each turn shows its prompt, reasoning, every tool call with latency and pass/fail, what
came back, and per-turn tokens and cost. Both flag failures, **inferred** retries (a
failed call followed by a near-identical one — nothing in either transcript records a
retry), and results heavier than that session's own p90. The Codex narrative additionally
marks compaction, aborted turns, thread rollbacks, and sub-agent spawns inline.

One structural difference worth knowing: Codex stamps `turn_id` on every reasoning,
message, tool-call and patch record, so its turn boundaries are read directly. Claude's
transcript has no such field, so its narrative resolves each entry to the owning prompt
through the `parentUuid` chain.

How the numbers are built:

- **Claude Code:** each assistant message carries `usage` and `model`; the analyzer sums
  per-message input/output/cache-write/cache-read buckets.
- **Codex:** each rollout carries cumulative `token_count` snapshots. The analyzer keeps the
  final snapshot for that rollout, attributes positive deltas to the active turn model, and
  follows `parent_thread_id` across separately stored sub-agent rollouts.
- Codex `cached_input_tokens` is a subset of `input_tokens`, and
  `reasoning_output_tokens` is a subset of `output_tokens`. The analyzer subtracts cached
  input before applying the full input rate and bills output once; the previous analyzer
  incorrectly added both subsets a second time.
- Both totals intentionally reflect repeated context sent across turns, rather than only the
  final context-window size.

> **Cost here is an estimate.** The pricing table needs maintenance and transcript schemas
> are internal/unstable (parsed defensively, fails soft). Codex private/internal model slugs
> without a published API rate display `N/A`; the analyzer never borrows another model's
> price. Provider billing and subscription usage are authoritative.

The estimate also does not model account-specific discounts or long-context
multipliers; use it for relative task comparison, not reconciliation.

Reach for Tier 2 when the question is *"what did that task I ran earlier cost, and where did
the tokens go — which sub-agent, which model?"*

### Tier 3 — Historical dashboards

A local Docker stack captures the native OpenTelemetry exports from Claude Code and Codex
and graphs them together.

```
Claude Code ─┐
             ├─ OTLP ─▶ OpenTelemetry Collector ─▶ Prometheus ─▶ Grafana
Codex ───────┘          (:4317/:4318)             (:9090)       (:3000)
```

```bash
ai-telemetry up             # start collector + prometheus + grafana (alias: ai-obs)
codex-telemetry status      # provider-specific compatibility name
claude-telemetry logs       # follow the same collector
ai-telemetry down           # stop & remove
```

Grafana at **http://localhost:3000** auto-provisions two dashboards:

- **Claude Code — Tokens & Cost**: token/cost/model trends, active time, and edits.
- **Codex — Tokens, Agents & Tools**: tokens by type/model, tool outcomes, turn latency,
  and multi-agent spawns.

Claude telemetry is enabled by `.claude/settings.json`. Codex telemetry is enabled by the
native `[otel]` block in `.codex/config.toml`; `deploy.sh` merges that block into the live
config without replacing projects/plugins/MCP settings. Codex raw prompt export stays off.
When the stack is down, export fails asynchronously without blocking a task. Prometheus
counters re-baseline when the collector restarts; named Prometheus/Grafana volumes preserve
stored history and dashboard state across normal container recreation.

Metric names can shift between client/collector versions, so dashboards match series by
regex (for example `claude_code_token_usage.*` and `codex_turn_token_usage.*_sum`). If a
panel is empty, open Prometheus `/graph`, search for `claude_code` or `codex_`, and adjust.
Details: [`.claude/observability/README.md`](../.claude/observability/README.md).

Reach for Tier 3 when the question is *"am I improving — is my cost-per-week going down, and
what's my model mix and edit-acceptance trend?"*

## Which tier do I use?

- "What's this task doing **right now**?" → **Tier 1** (native usage/context UI)
- "What did **that task** cost, broken down by agent/model?" → **Tier 2** (analyzer digest)
- "Am I **improving over time**? Trends across sessions?" → **Tier 3** (Grafana)

## Maintenance

- **Pricing** (Tier 2) is **data, not code**: `shared/pricing/models.json`, deployed next
  to each analyzer. When Anthropic/OpenAI change rates, edit that file — or better, your
  own `~/.config/ai-tools/pricing.json`, which overrides it and survives `deploy.sh`.
  Check what's in effect with `--pricing` on either analyzer.

  A model with no configured price is reported **N/A and named**, never charged at a
  neighbouring model's rate. This matters in practice: internal slugs (`gpt-5.6-sol`) have
  no public price, and the Claude analyzer previously fell back to Opus rates for anything
  it didn't recognize — which produced confident, plausible, wrong numbers for every new
  model. Wildcards are suffix-safe (`gpt-5*` will not absorb `gpt-5.6`) so a new minor
  version stays visibly unpriced instead of silently inheriting the old rate.
- **Metric names** (Tier 3) — re-verify against Claude's
  [monitoring-usage docs](https://code.claude.com/docs/en/monitoring-usage) and Codex's
  [OTEL catalog](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry)
  after client upgrades.
- **The rule** — before adding any new hook, log, or panel, name which of the two questions
  it answers. If it answers neither, don't add it.
