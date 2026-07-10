# Codex Session Analyzer

Offline history and trace viewer for Codex CLI/Desktop rollout JSONL. It reads
`$CODEX_HOME/sessions` and `archived_sessions`, reconstructs sub-agent topology
from `parent_thread_id`, and never sends transcript data anywhere.

## Commands

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
```

IDs may be unique prefixes. A rollout JSONL path can be passed directly.

## Current Codex schema handling

- Canonical visible user/assistant messages come from `event_msg`; mirrored
  `response_item.message` records are deduplicated.
- Function, custom/freeform, web-search, and tool-search call/output records are
  normalized into one timeline.
- New/unknown lifecycle records are retained generically instead of crashing.
- `session_meta.parent_thread_id` links separately stored sub-agent rollouts.
- Final cumulative `token_count` snapshots provide per-rollout totals; positive
  deltas are attributed to the active `turn_context.model`.

Codex token fields have subset semantics: cached input is already inside input,
and reasoning output is already inside output. The analyzer does not add either
subset again.

## Cost caveat

Cost is an API-rate estimate for model IDs with a published rate in the embedded
table. Private/internal Codex slugs display `N/A` rather than using a guessed
fallback. Long-context or account-specific pricing adjustments are not modeled;
provider billing/subscription usage is authoritative.
