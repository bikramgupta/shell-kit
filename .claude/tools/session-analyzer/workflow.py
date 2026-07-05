#!/usr/bin/env python3
"""Claude Code Workflow Analyzer — describe dynamic workflow runs (wf_*.json).

Claude Code's Workflow tool persists each run as a snapshot at
  ~/.claude/projects/<project>/<session-id>/workflows/wf_<runId>.json
with the orchestration script under workflows/scripts/ and per-agent
transcripts + journal under <session-id>/subagents/workflows/<runId>/.

This tool pretty-prints those snapshots from the shell, outside any session.

Usage:
  claude-workflow-analyzer                     # List all workflow runs (all projects)
  claude-workflow-analyzer --list              # Same as above
  claude-workflow-analyzer --latest            # Describe the most recent run
  claude-workflow-analyzer wf_9e75b43c-855     # Describe a run by id (searched globally)
  claude-workflow-analyzer /path/to/wf_x.json  # Describe a run by snapshot path
  claude-workflow-analyzer --latest --agents   # Include the full per-agent table
  claude-workflow-analyzer --latest --samples  # Include one sample agent per phase

Token note: the snapshot's totalTokens is the orchestrator-reported figure.
For transcript-derived tokens + cost estimates per workflow, use
`claude-session-analyzer <session-id> --overview`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def find_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def all_workflow_snapshots() -> list[Path]:
    """All wf_*.json snapshots across every project, oldest first."""
    return sorted(find_projects_dir().glob("*/*/workflows/wf_*.json"),
                  key=lambda f: f.stat().st_mtime)


def ms_to_min_sec(ms) -> str:
    if ms is None:
        return "-"
    sec = ms / 1000
    if sec < 60:
        return f"{sec:.1f}s"
    return f"{sec / 60:.1f}m"


def load_run(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def agent_prefix(label: str) -> str:
    """Group labels like 'analyze:claude-adapter' by their prefix."""
    return label.split(":", 1)[0] if ":" in label else label


_TOKEN_FIELDS = ("input_tokens", "output_tokens",
                 "cache_creation_input_tokens", "cache_read_input_tokens")


def transcript_breakdown(transcript: Path) -> dict:
    """Sum in/out/cache tokens from one agent transcript (transcript-derived,
    incl. cache — a different accounting than the snapshot's reported `tokens`)."""
    totals = {k: 0 for k in _TOKEN_FIELDS}
    try:
        with transcript.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") != "assistant":
                    continue
                u = (e.get("message") or {}).get("usage") or e.get("usage") or {}
                for k in _TOKEN_FIELDS:
                    totals[k] += u.get(k) or 0
    except OSError:
        pass
    totals["total"] = sum(totals[k] for k in _TOKEN_FIELDS)
    return totals


def breakdowns_by_agent_id(agents_dir: Path) -> dict:
    """Map agentId -> transcript token breakdown for a workflow's agents dir.
    Transcripts are named agent-<agentId>.jsonl next to their .meta.json."""
    out = {}
    if not agents_dir.is_dir():
        return out
    for t in agents_dir.glob("agent-*.jsonl"):
        agent_id = t.stem.replace("agent-", "", 1)
        out[agent_id] = transcript_breakdown(t)
    return out


def print_section(title: str) -> None:
    print()
    print(title)
    print("=" * len(title))


def session_of(path: Path) -> str:
    """Session id a snapshot belongs to (…/<session-id>/workflows/wf_x.json)."""
    return path.parent.parent.name


def project_of(path: Path) -> str:
    """Encoded project dir a snapshot belongs to."""
    return path.parent.parent.parent.name


def list_runs(paths: list[Path]) -> None:
    if not paths:
        print("No workflow runs found under ~/.claude/projects/*/*/workflows/")
        return
    print(f"{'RUN ID':<18} {'NAME':<28} {'STATUS':<10} {'AGENTS':>6} "
          f"{'TOKENS':>12} {'TIME':>7}  SESSION / PROJECT")
    print("-" * 110)
    for p in reversed(paths):  # newest first
        try:
            d = load_run(p)
        except (json.JSONDecodeError, OSError):
            print(f"{p.stem:<18} (unreadable snapshot: {p})")
            continue
        tokens = f"{d['totalTokens']:,}" if d.get("totalTokens") else "-"
        print(f"{d.get('runId', p.stem):<18} "
              f"{(d.get('workflowName') or '?')[:28]:<28} "
              f"{(d.get('status') or '?'):<10} "
              f"{d.get('agentCount', 0):>6} "
              f"{tokens:>12} "
              f"{ms_to_min_sec(d.get('durationMs')):>7}  "
              f"{session_of(p)[:8]} / {project_of(p)}")
    print()
    print("Describe one:  claude-workflow-analyzer <run-id>")


def summarize_run(data: dict, path: Path, show_agents: bool, show_samples: bool) -> None:
    print_section("WORKFLOW RUN SUMMARY")
    print(f"file:         {path}")
    print(f"runId:        {data.get('runId', '-')}")
    print(f"workflowName: {data.get('workflowName', '-')}")
    print(f"status:       {data.get('status', '-')}")
    print(f"session:      {session_of(path)}  ({project_of(path)})")
    print(f"duration:     {ms_to_min_sec(data.get('durationMs'))} ({data.get('durationMs')} ms)")
    print(f"agentCount:   {data.get('agentCount', '-')}")
    print(f"totalTokens:  {data.get('totalTokens', 0):,} (orchestrator-reported)"
          if data.get("totalTokens") else "totalTokens:  -")
    print(f"toolCalls:    {data.get('totalToolCalls', '-')}")
    print(f"defaultModel: {data.get('defaultModel', '-')}")
    if data.get("scriptPath"):
        print(f"scriptPath:   {data['scriptPath']}")

    args_val = data.get("args")
    if isinstance(args_val, str) and args_val.strip():
        preview = args_val.strip().replace("\n", " ")
        if len(preview) > 160:
            preview = preview[:157] + "..."
        print(f"args:         {preview}")
    elif args_val not in (None, ""):
        preview = json.dumps(args_val)
        print(f"args:         {preview[:160]}{'...' if len(preview) > 160 else ''}")

    phases = data.get("phases") or []
    if phases:
        print_section("DECLARED PHASES")
        for i, phase in enumerate(phases, 1):
            title = phase.get("title", "?")
            detail = phase.get("detail", "")
            model = phase.get("model", "")
            line = f"  {i}. {title}"
            if model:
                line += f"  [{model}]"
            print(line)
            if detail:
                print(f"     {detail}")

    logs = data.get("logs") or []
    if logs:
        print_section(f"ORCHESTRATION LOGS ({len(logs)})")
        for entry in logs:
            # Entries are strings or single-key dicts depending on version
            if isinstance(entry, dict):
                for k, v in entry.items():
                    print(f"  {k}" + (f": {v}" if v not in (None, "") else ""))
            else:
                print(f"  {entry}")

    progress = data.get("workflowProgress") or []
    phase_markers = [p for p in progress if p.get("type") == "workflow_phase"]
    agents = [p for p in progress if p.get("label")]

    if phase_markers:
        print_section("PHASE MARKERS")
        for marker in phase_markers:
            print(f"  {marker.get('index')}. {marker.get('title')}")

    if agents:
        print_section(f"AGENTS ({len(agents)})")
        by_phase = Counter(a.get("phaseTitle", "?") for a in agents)
        by_state = Counter(a.get("state", "?") for a in agents)
        by_prefix = Counter(agent_prefix(a.get("label", "")) for a in agents)

        print("  by phase:")
        for phase, count in sorted(by_phase.items(), key=lambda x: -x[1]):
            print(f"    {phase}: {count}")

        print("  by label prefix:")
        for prefix, count in sorted(by_prefix.items(), key=lambda x: -x[1]):
            print(f"    {prefix}: {count}")

        print("  by state:")
        for state, count in by_state.most_common():
            print(f"    {state}: {count}")

        models = sorted({a.get("model") for a in agents if a.get("model")})
        if models:
            print(f"  models: {', '.join(models)}")

        # Phase timeline in observed order (phase markers first, then any
        # phase titles only seen on agents)
        phase_order = [m.get("title") for m in phase_markers]
        for a in agents:
            title = a.get("phaseTitle", "?")
            if title not in phase_order:
                phase_order.append(title)

        print_section("PHASE TIMELINE")
        for phase in phase_order:
            group = [a for a in agents if a.get("phaseTitle", "?") == phase]
            if not group:
                continue
            durations = [a.get("durationMs") for a in group if a.get("durationMs") is not None]
            tokens = sum(a.get("tokens") or 0 for a in group)
            print(f"  {phase}:")
            print(f"    agents: {len(group)}")
            if tokens:
                print(f"    tokens (reported): {tokens:,}")
            if durations:
                print(f"    sum agent duration: {ms_to_min_sec(sum(durations))}")

        if show_agents:
            bd = breakdowns_by_agent_id(
                path.parent.parent / "subagents" / "workflows" / path.stem)
            print_section("AGENT TABLE")
            print("  REPORTED = orchestrator's own per-agent metric (excludes cached context).  "
                  "IN/OUT/CACHE-* = transcript-derived (incl. cache).")
            print(f"  {'LABEL':<26} {'PHASE':<10} {'REPORTED':>9} "
                  f"{'IN':>7} {'OUT':>7} {'CACHE-W':>9} {'CACHE-R':>10} {'TIME':>7}  MODEL")
            for a in sorted(agents, key=lambda a: (a.get("phaseIndex") or 0, a.get("index") or 0)):
                t = bd.get(a.get("agentId"), {})
                print(f"  {(a.get('label') or '?')[:26]:<26} "
                      f"{(a.get('phaseTitle') or '?')[:10]:<10} "
                      f"{a.get('tokens') or 0:>9,} "
                      f"{t.get('input_tokens', 0):>7,} "
                      f"{t.get('output_tokens', 0):>7,} "
                      f"{t.get('cache_creation_input_tokens', 0):>9,} "
                      f"{t.get('cache_read_input_tokens', 0):>10,} "
                      f"{ms_to_min_sec(a.get('durationMs')):>7}  "
                      f"{a.get('model') or '-'}")

        if show_samples:
            print_section("SAMPLE AGENTS (one per phase)")
            seen: set[str] = set()
            for a in agents:
                phase = a.get("phaseTitle", "?")
                if phase in seen:
                    continue
                seen.add(phase)
                print(f"  [{phase}] {a.get('label', '?')}")
                print(f"    state={a.get('state')} tokens={a.get('tokens')} "
                      f"toolCalls={a.get('toolCalls')} duration={ms_to_min_sec(a.get('durationMs'))}")
                prompt = (a.get("promptPreview") or "").replace("\n", " ")
                result = (a.get("resultPreview") or "").replace("\n", " ")
                if prompt:
                    print(f"    prompt: {prompt[:120]}{'...' if len(prompt) > 120 else ''}")
                if result:
                    print(f"    result: {result[:120]}{'...' if len(result) > 120 else ''}")

    result = data.get("result")
    if result is not None:
        print_section("FINAL RESULT")
        if isinstance(result, dict):
            for key, value in result.items():
                if isinstance(value, list):
                    print(f"  {key}: list[{len(value)}]")
                elif isinstance(value, dict):
                    print(f"  {key}: dict[{len(value)} keys]")
                else:
                    preview = str(value).replace("\n", " ")
                    print(f"  {key}: {preview[:200]}{'...' if len(preview) > 200 else ''}")
        else:
            preview = str(result).replace("\n", " ")
            print(f"  {preview[:400]}{'...' if len(preview) > 400 else ''}")

    # Companion artifacts on disk
    agents_dir = path.parent.parent / "subagents" / "workflows" / path.stem
    journal = agents_dir / "journal.jsonl"
    print_section("ON-DISK ARTIFACTS")
    print(f"  snapshot:    {path}")
    if data.get("scriptPath"):
        print(f"  script:      {data['scriptPath']}")
    if agents_dir.is_dir():
        transcripts = sorted(agents_dir.glob("agent-*.jsonl"))
        print(f"  agents dir:  {agents_dir}  ({len(transcripts)} transcripts)")
    if journal.is_file():
        types = Counter()
        with journal.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        types[json.loads(line).get("type", "?")] += 1
                    except json.JSONDecodeError:
                        types["(unparseable)"] += 1
        counts = ", ".join(f"{t}={n}" for t, n in types.most_common())
        print(f"  journal:     {journal}  ({counts})")
    print()
    print("  Transcript-derived tokens + cost: "
          f"claude-session-analyzer {session_of(path)[:8]} --overview")


def resolve_inputs(inputs: list[str], latest: bool) -> list[Path]:
    """Resolve CLI inputs to snapshot paths. Bare run ids are searched globally."""
    if latest and not inputs:
        snaps = all_workflow_snapshots()
        if not snaps:
            raise SystemExit("No workflow runs found under ~/.claude/projects/")
        return [snaps[-1]]

    paths = []
    snaps = None
    for item in inputs:
        p = Path(item).expanduser()
        if p.is_file():
            paths.append(p.resolve())
            continue
        # Treat as a run id — search all projects
        if snaps is None:
            snaps = all_workflow_snapshots()
        matches = [s for s in snaps if item in s.stem]
        if not matches:
            raise SystemExit(f"No snapshot found for '{item}' "
                             f"(searched ~/.claude/projects/*/*/workflows/)")
        paths.extend(matches)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Describe Claude Code dynamic workflow runs (wf_*.json snapshots).")
    parser.add_argument("inputs", nargs="*",
                        help="Snapshot path(s) or run id(s) (e.g. wf_9e75b43c-855). "
                             "Default: list all runs.")
    parser.add_argument("--list", action="store_true",
                        help="List all workflow runs across projects (newest first)")
    parser.add_argument("--latest", action="store_true",
                        help="Describe the most recently modified run")
    parser.add_argument("--agents", action="store_true",
                        help="Include the full per-agent table")
    parser.add_argument("--samples", action="store_true",
                        help="Include one sample agent per phase with prompt/result previews")
    args = parser.parse_args()

    if args.list or (not args.inputs and not args.latest):
        list_runs(all_workflow_snapshots())
        return

    paths = resolve_inputs(args.inputs, args.latest)
    for i, path in enumerate(paths):
        if i > 0:
            print("\n" + "#" * 72)
        try:
            summarize_run(load_run(path), path, args.agents, args.samples)
        except (json.JSONDecodeError, OSError) as e:
            print(f"error: cannot read {path}: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
