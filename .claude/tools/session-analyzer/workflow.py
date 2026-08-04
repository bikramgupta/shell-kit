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
  claude-workflow-analyzer <id> --mermaid      # Emit a Mermaid flowchart to stdout
  claude-workflow-analyzer <id> --diagram      # Write + open an HTML diagram (flow + timeline)
  claude-workflow-analyzer <id> --html --out x.html   # Write the HTML diagram to a path

Diagrams: every snapshot is normalized to the same shape — a run, an ordered
phase spine, and agents fanned out under each phase — so one data-driven layout
renders any workflow (2 agents or 200). Large fan-outs collapse to 'prefix ×N'
group nodes (Mermaid) / wrap into chips (HTML) to stay legible.

Token note: the snapshot's totalTokens is the orchestrator-reported figure.
For transcript-derived tokens + cost estimates per workflow, use
`claude-session-analyzer <session-id> --overview`.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import tempfile
import webbrowser
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


# ---------------------------------------------------------------------------
# Diagrams
#
# Every workflow snapshot — however large — has the same recoverable shape:
#   run  ->  ordered phase spine  ->  agents fanned out under each phase.
# `build_phase_model` normalizes any snapshot into that shape so a single
# data-driven layout renders 2 agents or 200. The renderers below (Mermaid
# text + self-contained HTML) never hard-code a workflow; they just walk it.
# ---------------------------------------------------------------------------

# state -> (mermaid classDef name, hex color) for consistent coloring
_STATE_STYLE = {
    "done":    ("adone", "#2ea043"),
    "error":   ("aerr",  "#e5534b"),
    "failed":  ("aerr",  "#e5534b"),
    "running": ("arun",  "#4da8da"),
    "queued":  ("aother", "#8b949e"),
}
# phase accent palette (cycles); readable on the dark theme
_PHASE_COLORS = ["#4da8da", "#e94560", "#f0a500", "#7c5cff",
                 "#2ea043", "#ff7ab6", "#00b8a3", "#c9a227"]


def _state_class(state: str) -> str:
    return _STATE_STYLE.get(state, ("aother", "#8b949e"))[0]


def _norm_agent(a: dict, start_time) -> dict:
    started = a.get("startedAt")
    rel = (started - start_time) if (started and start_time) else None
    return {
        "label": a.get("label") or "?",
        "prefix": agent_prefix(a.get("label") or ""),
        "state": a.get("state") or "?",
        "tokens": a.get("tokens") or 0,
        "toolCalls": a.get("toolCalls") or 0,
        "durationMs": a.get("durationMs"),
        "rel_start": rel,
        "model": a.get("model") or "",
        "index": a.get("index") or 0,
    }


def build_phase_model(data: dict) -> tuple[list[dict], int]:
    """Normalize a snapshot into an ordered list of phases, each with its
    agents (sorted by start time). Returns (phases, start_time)."""
    prog = data.get("workflowProgress") or []
    markers = [p for p in prog if p.get("type") == "workflow_phase"]
    agents_raw = [p for p in prog if p.get("label")]
    start_time = data.get("startTime")

    # Phase order: declared markers first, then any agent-only phase titles.
    order = [m.get("title") for m in markers]
    for a in agents_raw:
        t = a.get("phaseTitle", "?")
        if t not in order:
            order.append(t)

    declared = {ph.get("title"): ph for ph in (data.get("phases") or [])}

    phases = []
    for i, title in enumerate(order, 1):
        group = [_norm_agent(a, start_time)
                 for a in agents_raw if a.get("phaseTitle", "?") == title]
        group.sort(key=lambda x: (x["rel_start"] if x["rel_start"] is not None
                                  else 0, x["index"]))
        meta = declared.get(title, {})
        phases.append({
            "index": i,
            "title": title or "?",
            "detail": meta.get("detail", ""),
            "model": meta.get("model", ""),
            "agents": group,
            "tokens": sum(g["tokens"] for g in group),
            "duration_sum": sum(g["durationMs"] or 0 for g in group),
        })
    return phases, start_time


def _tok(n: int) -> str:
    """Compact token count: 1234 -> '1.2k', 999 -> '999'."""
    if n >= 1000:
        return f"{n / 1000:.0f}k" if n >= 10000 else f"{n / 1000:.1f}k"
    return str(n)


# --- Mermaid ---------------------------------------------------------------

def _mm_text(s: str) -> str:
    """Sanitize a string for use inside a quoted Mermaid node label."""
    return s.replace('"', "'").replace("\n", " ").strip()


def render_mermaid(data: dict, collapse_at: int = 10) -> str:
    """A Mermaid flowchart: phases down the spine, agents fanned out.
    Phases with more than `collapse_at` agents collapse to 'prefix ×N'
    group nodes so large fan-outs stay legible."""
    phases, _ = build_phase_model(data)
    name = data.get("workflowName") or data.get("runId") or "workflow"

    out = ["flowchart TD",
           f"  %% {name} — {data.get('status', '?')} — "
           f"{data.get('agentCount', '?')} agents",
           "  classDef phase fill:#0f3460,stroke:#4da8da,color:#fff,font-weight:bold;",
           "  classDef adone fill:#132e1a,stroke:#2ea043,color:#c9f7d4;",
           "  classDef aerr fill:#2e1414,stroke:#e5534b,color:#f7c9c9;",
           "  classDef arun fill:#0d2436,stroke:#4da8da,color:#cfe8f7;",
           "  classDef aother fill:#20232e,stroke:#8b949e,color:#d5d5d5;"]

    prev = None
    for ph in phases:
        pid = f"P{ph['index']}"
        head = f"{ph['index']}. {_mm_text(ph['title'])}"
        if ph["model"]:
            head += f" · {ph['model']}"
        head += f" · {len(ph['agents'])} agents"
        if ph["tokens"]:
            head += f" · {_tok(ph['tokens'])} tok"
        out.append(f'  {pid}["{head}"]:::phase')
        if prev:
            out.append(f"  {prev} --> {pid}")
        prev = pid

        if len(ph["agents"]) > collapse_at:
            counts = Counter(a["prefix"] for a in ph["agents"])
            for j, (pref, c) in enumerate(counts.most_common()):
                # dominant state of the group drives the color
                states = Counter(a["state"] for a in ph["agents"]
                                 if a["prefix"] == pref)
                cls = _state_class(states.most_common(1)[0][0])
                out.append(f'  {pid} --> {pid}g{j}'
                           f'["{_mm_text(pref)} ×{c}"]:::{cls}')
        else:
            for j, a in enumerate(ph["agents"]):
                extra = []
                if a["tokens"]:
                    extra.append(f"{_tok(a['tokens'])} tok")
                if a["durationMs"]:
                    extra.append(ms_to_min_sec(a["durationMs"]))
                sfx = ("<br/>" + " · ".join(extra)) if extra else ""
                out.append(f'  {pid} --> {pid}a{j}'
                           f'["{_mm_text(a["label"])}{sfx}"]'
                           f":::{_state_class(a['state'])}")
    return "\n".join(out)


# --- HTML ------------------------------------------------------------------

_HTML_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #1a1a2e; color: #eee; padding: 24px; line-height: 1.5; }
h1 { font-size: 1.5em; margin-bottom: 4px; }
h2 { font-size: 1.05em; color: #4da8da; margin: 28px 0 12px;
     text-transform: uppercase; letter-spacing: .05em; font-weight: 700; }
a { color: #4da8da; }
.header { background: #16213e; padding: 20px 24px; border-radius: 10px; }
.sub { color: #8b949e; font-size: .85em; font-family: ui-monospace, SFMono-Regular, monospace; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px;
         font-size: .75em; font-weight: 700; margin-left: 8px; vertical-align: middle; }
.badge.completed { background: #132e1a; color: #7ee2a0; border: 1px solid #2ea043; }
.badge.failed, .badge.error { background: #2e1414; color: #f7a9a9; border: 1px solid #e5534b; }
.badge.running { background: #0d2436; color: #9fd4f2; border: 1px solid #4da8da; }
.badge.other { background: #20232e; color: #cfcfcf; border: 1px solid #8b949e; }
.stats { display: flex; gap: 12px; margin-top: 16px; flex-wrap: wrap; }
.stat { background: #0f3460; padding: 10px 15px; border-radius: 8px; min-width: 84px; }
.stat .v { font-size: 1.25em; font-weight: 700; color: #e94560; }
.stat .l { font-size: .72em; color: #9fb3c8; text-transform: uppercase; letter-spacing: .04em; }

/* phase-flow strip */
.flow { display: flex; gap: 0; align-items: stretch; overflow-x: auto; padding-bottom: 8px; }
.pcard { background: #16213e; border-radius: 10px; padding: 14px 16px; min-width: 230px;
         flex: 1 1 0; border-top: 4px solid var(--accent); }
.pcard .pt { font-weight: 700; font-size: 1em; }
.pcard .pm { font-size: .72em; color: #9fb3c8; font-family: ui-monospace, monospace; margin: 2px 0 8px; }
.pcard .pnums { font-size: .78em; color: #cbd5e1; margin-bottom: 10px; }
.pcard .pnums b { color: #fff; }
.chips { display: flex; flex-wrap: wrap; gap: 5px; }
.chip { display: inline-flex; align-items: center; gap: 5px; background: #0f2036;
        border: 1px solid #233; border-radius: 6px; padding: 2px 7px; font-size: .72em;
        max-width: 100%; }
.chip .dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
.chip .n { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chip.more { color: #9fb3c8; font-style: italic; }
.arrow { display: flex; align-items: center; color: #4da8da; font-size: 1.6em; padding: 0 6px; flex: none; }

/* gantt — axis + rows share one grid so ticks line up with bars */
.gantt { background: #12182b; border-radius: 10px; padding: 16px; overflow-x: auto; }
.row { display: grid; grid-template-columns: 220px 1fr; align-items: center;
       height: 20px; gap: 8px; }
.axishead { height: 20px; }
.axis { position: relative; height: 18px; border-bottom: 1px solid #2a3550; }
.tick { position: absolute; top: 0; font-size: .68em; color: #7f8fa6; transform: translateX(-50%);
        border-left: 1px solid #2a3550; padding-left: 4px; height: 100%; }
.tick:first-child { transform: none; }
.lane-head { display: flex; align-items: center; gap: 8px; margin: 14px 0 6px; font-weight: 700; }
.lane-head .sw { width: 12px; height: 12px; border-radius: 3px; }
.lane-head .c { font-size: .74em; color: #9fb3c8; font-weight: 400; }
.row:hover { background: #1b2540; }
.row .g { font-size: .72em; color: #cbd5e1; overflow: hidden; text-overflow: ellipsis;
          white-space: nowrap; font-family: ui-monospace, monospace; }
.track { position: relative; height: 12px; }
.bar { position: absolute; height: 12px; border-radius: 3px; min-width: 2px; opacity: .92; }
.bar.err { outline: 2px solid #e5534b; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 12px; font-size: .76em; color: #9fb3c8; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend .d { width: 10px; height: 10px; border-radius: 50%; }
.foot { margin-top: 24px; color: #7f8fa6; font-size: .78em; font-family: ui-monospace, monospace; }
"""


def _dur_axis(total_ms: int, n: int = 5) -> list[tuple[float, str]]:
    return [(i / (n - 1) * 100.0, ms_to_min_sec(total_ms * i / (n - 1)))
            for i in range(n)]


def render_html(data: dict, path: Path) -> str:
    """Self-contained HTML: header stats + phase-flow strip + swimlane Gantt."""
    phases, _ = build_phase_model(data)
    esc = html.escape

    # total timeline span (ms), from agent start+duration; fall back to run duration
    spans = [(a["rel_start"] or 0) + (a["durationMs"] or 0)
             for ph in phases for a in ph["agents"] if a["rel_start"] is not None]
    total = max(spans) if spans else (data.get("durationMs") or 1)
    total = max(total, 1)

    status = (data.get("status") or "other").lower()
    badge_cls = status if status in ("completed", "failed", "error", "running") else "other"
    name = esc(data.get("workflowName") or data.get("runId") or "workflow")

    def stat(v, l):
        return f'<div class="stat"><div class="v">{v}</div><div class="l">{esc(l)}</div></div>'

    tok = data.get("totalTokens") or 0
    stats = "".join([
        stat(f"{data.get('agentCount', 0)}", "agents"),
        stat(f"{len(phases)}", "phases"),
        stat(f"{tok:,}" if tok else "-", "tokens"),
        stat(f"{data.get('totalToolCalls', 0)}", "tool calls"),
        stat(ms_to_min_sec(data.get("durationMs")), "duration"),
    ])

    # ---- phase-flow strip ----
    CHIP_CAP = 18
    flow_parts = []
    for i, ph in enumerate(phases):
        accent = _PHASE_COLORS[i % len(_PHASE_COLORS)]
        chips = []
        for a in ph["agents"][:CHIP_CAP]:
            _, color = _STATE_STYLE.get(a["state"], ("aother", "#8b949e"))
            title = (f'{a["label"]} — {a["state"]} · {_tok(a["tokens"])} tok · '
                     f'{a["toolCalls"]} tools · {ms_to_min_sec(a["durationMs"])}'
                     f'{" · " + a["model"] if a["model"] else ""}')
            chips.append(
                f'<span class="chip" title="{esc(title)}">'
                f'<span class="dot" style="background:{color}"></span>'
                f'<span class="n">{esc(a["label"])}</span></span>')
        extra = len(ph["agents"]) - CHIP_CAP
        if extra > 0:
            chips.append(f'<span class="chip more">+{extra} more</span>')
        model_line = esc(ph["model"] or ph["detail"] or "")
        flow_parts.append(
            f'<div class="pcard" style="--accent:{accent}">'
            f'<div class="pt">{ph["index"]}. {esc(ph["title"])}</div>'
            f'<div class="pm">{model_line}</div>'
            f'<div class="pnums"><b>{len(ph["agents"])}</b> agents · '
            f'<b>{_tok(ph["tokens"])}</b> tok</div>'
            f'<div class="chips">{"".join(chips)}</div></div>')
        if i < len(phases) - 1:
            flow_parts.append('<div class="arrow">&rarr;</div>')
    flow = "".join(flow_parts)

    # ---- gantt ----
    ticks = "".join(f'<div class="tick" style="left:{pct}%">{lbl}</div>'
                    for pct, lbl in _dur_axis(total))
    lanes = []
    for i, ph in enumerate(phases):
        accent = _PHASE_COLORS[i % len(_PHASE_COLORS)]
        lanes.append(
            f'<div class="lane-head"><span class="sw" style="background:{accent}"></span>'
            f'{esc(ph["title"])} <span class="c">· {len(ph["agents"])} agents · '
            f'{ms_to_min_sec(ph["duration_sum"])} total agent-time</span></div>')
        for a in ph["agents"]:
            left = (a["rel_start"] or 0) / total * 100.0
            width = max((a["durationMs"] or 0) / total * 100.0, 0.4)
            errcls = " err" if a["state"] in ("error", "failed") else ""
            title = (f'{a["label"]} — {a["state"]} · {_tok(a["tokens"])} tok · '
                     f'{a["toolCalls"]} tools · {ms_to_min_sec(a["durationMs"])}'
                     f'{" · " + a["model"] if a["model"] else ""}')
            lanes.append(
                f'<div class="row" title="{esc(title)}">'
                f'<div class="g">{esc(a["label"])}</div>'
                f'<div class="track"><div class="bar{errcls}" '
                f'style="left:{left:.3f}%;width:{width:.3f}%;background:{accent}"></div>'
                f'</div></div>')
    gantt_body = "".join(lanes)

    legend = "".join(
        f'<span><span class="d" style="background:{c}"></span>{esc(s)}</span>'
        for s, (_, c) in [(k, v) for k, v in _STATE_STYLE.items()
                          if k in ("done", "running", "error", "queued")])

    session = session_of(path)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Workflow: {name}</title>
<style>{_HTML_CSS}</style></head><body>
<div class="header">
  <h1>{name}<span class="badge {badge_cls}">{esc(data.get('status', '?'))}</span></h1>
  <div class="sub">{esc(data.get('runId', '-'))} · {esc(session[:8])} · {esc(data.get('defaultModel', '-'))}</div>
  <div class="stats">{stats}</div>
</div>

<h2>Phase flow</h2>
<div class="flow">{flow}</div>

<h2>Timeline</h2>
<div class="gantt">
  <div class="row axishead"><div></div><div class="axis">{ticks}</div></div>
  <div>{gantt_body}</div>
  <div class="legend">{legend}
    <span>bars colored by phase · outlined bars errored · hover for detail</span>
  </div>
</div>

<div class="foot">{esc(str(path))}<br/>
Transcript-derived tokens + cost: claude-session-analyzer {esc(session[:8])} --overview</div>
</body></html>"""


def write_diagram(data: dict, path: Path, out: str | None, do_open: bool) -> None:
    """Render the HTML diagram to a file and optionally open it."""
    run_id = data.get("runId") or path.stem
    if out:
        dest = Path(out).expanduser().resolve()
    else:
        dest = Path(tempfile.gettempdir()) / f"{run_id}.diagram.html"
    dest.write_text(render_html(data, path), encoding="utf-8")
    print(f"Wrote diagram: {dest}")
    if do_open:
        webbrowser.open(dest.as_uri())


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
    parser.add_argument("--mermaid", action="store_true",
                        help="Emit a Mermaid flowchart of the run to stdout (portable, "
                             "drops into markdown)")
    parser.add_argument("--html", action="store_true",
                        help="Write a self-contained HTML diagram (phase flow + timeline)")
    parser.add_argument("--diagram", action="store_true",
                        help="Write the HTML diagram and open it in the browser "
                             "(shorthand for --html --open)")
    parser.add_argument("--open", action="store_true",
                        help="Open the HTML diagram in the browser (with --html)")
    parser.add_argument("--out", metavar="FILE",
                        help="Output path for --html/--diagram (default: a temp file)")
    args = parser.parse_args()

    if args.list or (not args.inputs and not args.latest):
        list_runs(all_workflow_snapshots())
        return

    want_html = args.html or args.diagram
    do_open = args.open or args.diagram

    paths = resolve_inputs(args.inputs, args.latest)
    for i, path in enumerate(paths):
        if i > 0 and not (args.mermaid or want_html):
            print("\n" + "#" * 72)
        try:
            data = load_run(path)
        except (json.JSONDecodeError, OSError) as e:
            print(f"error: cannot read {path}: {e}", file=sys.stderr)
            sys.exit(1)
        if args.mermaid:
            print(render_mermaid(data))
        if want_html:
            write_diagram(data, path, args.out, do_open)
        if not (args.mermaid or want_html):
            summarize_run(data, path, args.agents, args.samples)


if __name__ == "__main__":
    main()
