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
| **1 — Live** | `/usage`, `/cost`, `statusline.sh` | none (native) | current session tokens & cost, right now | **Yes** — `/cost` and the Usage API are ground truth |
| **2 — Offline** | `claude-session-analyzer` / `codex-session-analyzer` | none (reads local transcripts) | per-session / per-agent / per-model token + cost for any past session | Estimate |
| **3 — Historical** | OTEL → Prometheus → Grafana stack | `claude-telemetry up` (Docker) | trends across *all* sessions over time | Estimate (metrics), cost is Claude-reported |

### Tier 1 — Live (native)

Nothing to install. In any session:

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
```

How the numbers are built:

- Each assistant message carries a `usage` object (`input_tokens`, `output_tokens`,
  `cache_creation_input_tokens`, `cache_read_input_tokens`) and a `model`. The analyzer sums
  these across the transcript.
- Summing per-message `usage` intentionally reflects **billed** input per turn — context is
  re-sent each turn, and cache-reads are discounted — so the total mirrors real cost, not
  just the final context size.
- Cost = `input·rate_in + output·rate_out + cache_write·rate_in·1.25 + cache_read·rate_in·0.10`,
  from an embedded per-model pricing table.

> **Cost here is an estimate.** The pricing table needs occasional maintenance and the
> transcript schema is internal/unstable (parsed defensively, fails soft). For anything that
> must be exact, `/cost` and the Usage API are authoritative. Cross-check a session's
> analyzer total against `/cost` if precision matters.

Reach for Tier 2 when the question is *"what did that task I ran earlier cost, and where did
the tokens go — which sub-agent, which model?"*

### Tier 3 — Historical dashboards

A local, self-contained Docker stack captures Claude Code's native OpenTelemetry export and
graphs it over time. Nothing leaves your machine.

```
Claude Code ──OTLP──▶ OpenTelemetry Collector ──scrape──▶ Prometheus ──▶ Grafana
 (:4317/:4318)             (re-exposes :8889)               (:9090)       (:3000)
```

```bash
claude-telemetry up        # start collector + prometheus + grafana (alias: cc-obs)
claude-telemetry status    # container health
claude-telemetry logs      # follow the collector (raw exported events)
claude-telemetry down      # stop & remove
```

Then open Grafana at **http://localhost:3000** (anonymous admin) → dashboard
**"Claude Code — Tokens & Cost"**: token usage over time by model, cost over time, tool
accept/reject rate, active time, session count.

Telemetry is turned on by the `env` block in `.claude/settings.json`
(`CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_*`). **When the stack is down, the OTLP export fails
silently in the background** — it never blocks, slows, or errors a session. Start the stack
only when you want to collect; there's no need to keep it running. (Prometheus counters
re-baseline whenever the collector restarts.)

Metric names can shift between Claude Code versions, so the bundled dashboard matches series
by regex (e.g. `claude_code_token_usage.*`) rather than hard-coding suffixes. If a panel is
empty, open Prometheus `/graph`, type `claude_code` to see the real series names, and adjust.
Details: [`.claude/observability/README.md`](../.claude/observability/README.md).

Reach for Tier 3 when the question is *"am I improving — is my cost-per-week going down, and
what's my model mix and edit-acceptance trend?"*

## Which tier do I use?

- "What's this session costing **right now**?" → **Tier 1** (`/usage`, `/cost`, statusline)
- "What did **that task** cost, broken down by agent/model?" → **Tier 2** (analyzer digest)
- "Am I **improving over time**? Trends across sessions?" → **Tier 3** (Grafana)

## Maintenance

- **Pricing tables** (Tier 2) live in each analyzer's `parser.py`. When Anthropic/OpenAI
  change rates, update the per-MTok dicts. They're clearly labeled estimates.
- **Metric names** (Tier 3) — re-verify against the
  [monitoring-usage docs](https://code.claude.com/docs/en/monitoring-usage) if a panel goes
  blank after a Claude Code upgrade.
- **The rule** — before adding any new hook, log, or panel, name which of the two questions
  it answers. If it answers neither, don't add it.
