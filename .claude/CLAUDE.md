# Personal Defaults

These preferences apply to all projects unless overridden by project-specific CLAUDE.md.

## Environment Detection

Before starting work, detect the environment:

```bash
# Check if running in dev container
if [ -f /.dockerenv ] || [ -n "$REMOTE_CONTAINERS" ] || [ -n "$CODESPACES" ]; then
  echo "Running in dev container - isolated environment"
else
  echo "Running on host - exercise caution"
fi
```

- **Dev container:** Full autonomy, safe to run commands freely
- **Host machine:** Be more cautious with system-level operations

## Code Quality

- Run tests before suggesting code is complete
- Always use TypeScript strict mode unless explicitly told otherwise
- Prefer functional programming patterns when appropriate
- Keep solutions simple - avoid over-engineering

## Workflow

- I typically run with `--dangerously-skip-permissions` in dev containers
- Environment variables are auto-loaded **per project** at session start: the
  SessionStart hook sources `.env` and `.env.local` from the current git root (or cwd).
  Each project keeps its own env; nothing is read from `$HOME`.
