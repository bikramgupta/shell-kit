# Codex Session Analyzer

Offline history, trace, and narrative viewer for Codex CLI/Desktop rollout JSONL.
It reads `$CODEX_HOME/sessions` and `archived_sessions`, reconstructs sub-agent
topology, and never sends transcript data anywhere.

Two tools, matching the Claude side:

| Tool | Question it answers |
|---|---|
| `codex-session-analyzer` | What happened across my sessions? Which ran, how long, how many agents, how many tokens, what did it cost? |
| `codex-session-narrative` | What happened *inside* one session? Prompt → reasoning → each tool call → what it returned → reply. |

## codex-session-analyzer

```bash
codex-session-analyzer                         # newest root task in the current cwd
codex-session-analyzer --latest                # newest root task across projects
codex-session-analyzer --list                  # root task history; agents/tokens/cost columns
codex-session-analyzer --list --all            # include standalone sub-agent rollouts
codex-session-analyzer --project /path --list  # exact cwd filter
codex-session-analyzer <id> --path             # resolve rollout JSONL path
codex-session-analyzer <id> --overview         # main + descendants + model/token totals
codex-session-analyzer <id> --digest           # markdown continuation digest
codex-session-analyzer <id> --open             # combined HTML trace with agent filter
codex-session-analyzer <id> --output json      # normalized machine-readable trace
codex-session-analyzer --pricing               # resolved price table + where it came from
```

IDs may be unique prefixes. A rollout JSONL path can be passed directly.

## codex-session-narrative

Reads one session as a story rather than an event list.

```bash
codex-session-narrative                        # latest session in this cwd, opens HTML
codex-session-narrative <id>                   # a specific session (id or prefix)
codex-session-narrative <path/to.jsonl>        # a rollout by path
codex-session-narrative --text                 # terminal rendering instead of HTML
codex-session-narrative <id> --out-file r.html # write somewhere specific
```

Per turn it shows the prompt, the reasoning, every tool call with its latency and
whether it succeeded, what came back, and the reply — plus per-turn tokens and
cost. It flags:

- **failures** — a non-zero exit, `Script failed`, a failed patch, an MCP `Err`
- **inferred retries** — a failed call followed by a near-identical one
  (nothing in the rollout marks a retry, so this is a heuristic)
- **heavy results** — tool output above this session's own p90, since a handful
  of huge results usually explains a surprising token bill
- **compaction, aborts, rollbacks, and sub-agent spawns**, inline where they
  happened

### Turn boundaries are exact here

Codex stamps `turn_id` on every record that matters — reasoning, messages, tool
calls and their outputs, patches. So turns are read directly, not reconstructed.
(The Claude narrative has no equivalent field and has to resolve each entry to
its owning prompt through the `parentUuid` chain.)

## Current Codex schema handling

- Canonical visible user/assistant messages come from `event_msg`; mirrored
  `response_item.message` records are deduplicated. Reasoning is mirrored into
  **both** `response_item.reasoning` and `event_msg.agent_reasoning`; the
  narrative keeps one copy.
- **`exec` scripts are decoded, not scraped.** Codex's dominant tool takes a
  JavaScript program as its input (`tools.shell_command({command, workdir})`),
  which is not JSON. The parser walks the call sites and reads the real command.
- `patch_apply_end` gives per-file add/update plus success — Codex's equivalent
  of a structured patch. Patches applied inline via an `exec` `*** Begin Patch`
  envelope are recognized too.
- `mcp_tool_call_end` gives server, tool, arguments, duration, and Ok/Err. An
  `Ok` whose body carries an error (a 404 in the text, say) counts as failed.
- `sub_agent_activity` links spawned agents by `agent_thread_id` and names them
  from `agent_path`; `session_meta.parent_thread_id` links separately stored
  sub-agent rollouts.
- `thread_settings_applied` records model and reasoning-effort changes
  explicitly, so model switches are read rather than inferred.
- `compacted` / `context_compacted`, `turn_aborted`, `thread_rolled_back` are
  reported instead of being silently dropped.
- `token_count.rate_limits` surfaces plan type and quota burn.
- New/unknown record families are retained generically instead of crashing.

Codex token fields have **subset** semantics: cached input is already inside
input, and reasoning output is already inside output. Neither is added again.

Two success signals matter and only using one gives the wrong answer:
`Script completed` / `Script failed` is the JS wrapper's own outcome, while
`Exit code: N` is the shell command inside it. A script can complete cleanly
while its command exits 1, so the command's exit code wins when present.

## Pricing

Prices are **data, not code** — see `shared/pricing/models.json`. Sources merge
in this order, later winning:

1. `models.json` shipped next to the analyzer
2. `~/.config/ai-tools/pricing.json` — your overrides; survives `deploy.sh`
3. `$AI_MODEL_PRICING`
4. `--pricing-file FILE`

A model with **no configured price is reported as N/A and named** — it is never
charged at a neighbouring model's rate. That is deliberate: internal slugs like
`gpt-5.6-sol` have no public price, and a confident wrong number is worse than
an honest gap. To price one:

```bash
mkdir -p ~/.config/ai-tools
cat > ~/.config/ai-tools/pricing.json <<'EOF'
{"models": {"gpt-5.6*": {"input": 1.75, "output": 14.0, "cache_read": 0.175}}}
EOF
```

Wildcards are suffix-safe: `gpt-5*` matches `gpt-5-codex` and `gpt-5-2026-01-01`
but **not** `gpt-5.6`, so a new minor version stays unpriced until you price it.
Use `gpt-5**` if you really do want to pin a whole family.

Cost is an API-rate estimate. Provider billing and subscription usage are
authoritative.
