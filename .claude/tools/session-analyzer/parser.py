#!/usr/bin/env python3
"""
Claude Code Session Analyzer

Parses session transcripts (main + subagent files) and generates
a unified timeline with agent attribution.

Usage:
    python3 parser.py [session-id]              # Analyze specific session
    python3 parser.py --latest                  # Analyze most recent session
    python3 parser.py --latest --project /path  # Most recent in specific project
    python3 parser.py --output json|html        # Output format (default: html)
    python3 parser.py --open                    # Open HTML in browser
    python3 parser.py --list                    # List all sessions with summaries
    python3 parser.py --digest                  # Generate markdown digest for follow-up
"""

import json
import argparse
import sys
import re
from pathlib import Path
from datetime import datetime
from typing import Optional
import subprocess
import tempfile


# Hook events to include (lifecycle events not in JSONL transcripts)
HOOK_EVENTS = {
    "SessionStart", "SessionEnd", "Stop", "SubagentStop",
    "PreCompact", "Notify", "Notification", "Permission", "PostToolUse"
}

# Regex pattern for hook log lines: [HH:MM:SS] [agent_id] EventType(optional): details
HOOK_LOG_PATTERN = re.compile(
    r'\[(\d{2}:\d{2}:\d{2})\]\s+\[([^\]]+)\]\s+(\w+)(?:\(([^)]+)\))?:\s*(.*)$'
)


def find_hook_logs(session_start: str, session_end: str,
                   project_path: Optional[str] = None) -> Optional[tuple[Path, Optional[Path]]]:
    """
    Find hook log files that overlap with the session time window.

    Args:
        session_start: ISO timestamp of session start
        session_end: ISO timestamp of session end
        project_path: Project directory path

    Returns:
        (main_log, agents_log) tuple or None if not found.
        agents_log may be None if only main log exists.
    """
    if not session_start:
        return None

    # Parse session start time
    try:
        session_start_dt = datetime.fromisoformat(session_start.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

    # Search locations for .claude-logs/
    search_paths = []
    if project_path:
        search_paths.append(Path(project_path) / ".claude-logs")
    search_paths.append(Path.home() / ".claude" / ".claude-logs")

    for logs_dir in search_paths:
        if not logs_dir.exists():
            continue

        # Find log files (exclude -agents.log suffix for main logs)
        log_files = sorted(
            [f for f in logs_dir.glob("*.log")
             if not f.name.endswith("-agents.log") and not f.name.startswith(".")],
            key=lambda f: f.name,
            reverse=True  # Newest first
        )

        for log_file in log_files:
            # Parse timestamp from filename: YYYYMMDD_HHMMSS.log
            try:
                filename = log_file.stem  # e.g., "20251231_113235"
                log_dt = datetime.strptime(filename, "%Y%m%d_%H%M%S")

                # Check if log file time is within reasonable range of session
                # (within 24 hours before session start to session end)
                time_diff = (session_start_dt.replace(tzinfo=None) - log_dt).total_seconds()
                if -86400 <= time_diff <= 86400:  # Within 24 hours
                    # Found a matching log file
                    agents_log = logs_dir / f"{filename}-agents.log"
                    return (log_file, agents_log if agents_log.exists() else None)
            except ValueError:
                continue  # Skip files that don't match the naming pattern

    return None


def parse_hook_log(log_path: Path, log_date: str) -> list[dict]:
    """
    Parse hook log file into list of events.

    Args:
        log_path: Path to .log file
        log_date: Date string (YYYY-MM-DD) derived from filename

    Returns:
        List of event dicts with: timestamp, agent, event_type, details, source
    """
    events = []

    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("="):  # Skip empty lines and separators
                    continue

                match = HOOK_LOG_PATTERN.match(line)
                if not match:
                    continue

                time_str, agent_id, event_type, param, details = match.groups()

                # Only include events we care about
                if event_type not in HOOK_EVENTS:
                    continue

                # Build ISO timestamp from log_date and time
                timestamp = f"{log_date}T{time_str}"

                event = {
                    "timestamp": timestamp,
                    "agent": agent_id,
                    "event_type": event_type,
                    "details": details.strip() if details else "",
                    "source": "hook",
                }

                # Handle PostToolUse specially - include tool name and status
                if event_type == "PostToolUse" and param:
                    event["tool_name"] = param
                    event["status"] = "error" if "error" in details.lower() else "success"

                events.append(event)

    except (OSError, IOError) as e:
        print(f"Warning: Could not read hook log {log_path}: {e}", file=sys.stderr)

    return events


def date_from_log_filename(log_path: Path) -> str:
    """Extract date string (YYYY-MM-DD) from log filename."""
    # Filename format: YYYYMMDD_HHMMSS.log
    filename = log_path.stem
    try:
        return f"{filename[:4]}-{filename[4:6]}-{filename[6:8]}"
    except (IndexError, ValueError):
        return datetime.now().strftime("%Y-%m-%d")


def encode_project_path(path: str) -> str:
    """Convert project path to Claude's encoded format."""
    # Both / and . are replaced with -
    return path.replace("/", "-").replace(".", "-")


def find_projects_dir() -> Path:
    """Find the Claude projects directory."""
    return Path.home() / ".claude" / "projects"


def find_agent_files(project_dir: Path, main_file: Path) -> list[Path]:
    """Find subagent transcripts belonging to a session.

    New layout (nested, per-session): <project>/<session-id>/subagents/agent-*.jsonl,
    with workflow-spawned agents one level deeper under
    subagents/workflows/<run-id>/agent-*.jsonl.
    Legacy layout: flat agent-*.jsonl files next to the main transcript.
    """
    agent_files = list(project_dir.glob("agent-*.jsonl"))  # legacy flat layout
    subagents_dir = project_dir / main_file.stem / "subagents"
    if subagents_dir.is_dir():
        agent_files.extend(subagents_dir.glob("agent-*.jsonl"))
        agent_files.extend(subagents_dir.glob("workflows/*/agent-*.jsonl"))
    return sorted(agent_files, key=lambda f: f.stat().st_mtime)


def find_session_files(session_id: Optional[str] = None,
                       project_path: Optional[str] = None,
                       latest: bool = False) -> tuple[Path, list[Path]]:
    """
    Find main session file and associated agent files.

    session_id may also be a filesystem path: either a transcript .jsonl or a
    per-session directory (<project>/<session-id>/) — used directly, no search.

    Returns:
        (main_transcript, list_of_agent_transcripts)
    """
    if session_id:
        p = Path(session_id).expanduser()
        if p.is_dir() and p.with_suffix(".jsonl").is_file():
            p = p.with_suffix(".jsonl")  # per-session dir → sibling transcript
        if p.suffix == ".jsonl" and p.is_file():
            p = p.resolve()
            return p, find_agent_files(p.parent, p)

    projects_dir = find_projects_dir()
    project_dir = None

    if project_path:
        encoded = encode_project_path(project_path)
        project_dir = projects_dir / encoded
        if not project_dir.exists():
            # Try with leading dash
            project_dir = projects_dir / f"-{encoded.lstrip('-')}"
        if not project_dir.exists():
            project_dir = None
    else:
        # Use current directory's project
        cwd = Path.cwd()
        encoded = encode_project_path(str(cwd))
        project_dir = projects_dir / encoded
        if not project_dir.exists():
            project_dir = projects_dir / f"-{encoded.lstrip('-')}"
        if not project_dir.exists():
            project_dir = None

    # If searching by session_id and no local project, search globally
    if session_id and not project_dir:
        print(f"Searching all projects for session {session_id}...", file=sys.stderr)
        for pdir in projects_dir.iterdir():
            if not pdir.is_dir():
                continue
            matches = [f for f in pdir.glob("*.jsonl")
                      if not f.name.startswith("agent-") and session_id in f.stem]
            if matches:
                project_dir = pdir
                break

    # Also search globally if session_id provided but not found locally
    if session_id and project_dir:
        local_matches = [f for f in project_dir.glob("*.jsonl")
                        if not f.name.startswith("agent-") and session_id in f.stem]
        if not local_matches:
            print(f"Session not in current project, searching all projects...", file=sys.stderr)
            for pdir in projects_dir.iterdir():
                if not pdir.is_dir():
                    continue
                matches = [f for f in pdir.glob("*.jsonl")
                          if not f.name.startswith("agent-") and session_id in f.stem]
                if matches:
                    project_dir = pdir
                    break

    if not project_dir or not project_dir.exists():
        raise FileNotFoundError(f"No project directory found with sessions")

    # Find main session files (exclude agent-*.jsonl)
    session_files = sorted(
        [f for f in project_dir.glob("*.jsonl") if not f.name.startswith("agent-")],
        key=lambda f: f.stat().st_mtime,
        reverse=True
    )

    if not session_files:
        raise FileNotFoundError(f"No session files found in {project_dir}")

    if latest:
        main_file = session_files[0]
    elif session_id:
        matches = [f for f in session_files if session_id in f.stem]
        if not matches:
            raise FileNotFoundError(f"Session {session_id} not found")
        main_file = matches[0]
    else:
        main_file = session_files[0]  # Default to latest

    # Find associated agent files (nested per-session layout + legacy flat)
    agent_files = find_agent_files(project_dir, main_file)

    return main_file, agent_files


def extract_session_summary(filepath: Path) -> dict:
    """Extract summary and metadata from a session file."""
    session_id = filepath.stem
    summary = ""
    start_time = None
    end_time = None
    project = None
    # Token/cost totals for this transcript (main agent only — subagent
    # transcripts are separate files; the digest/HTML show the full breakdown).
    tok_totals = {k: 0 for k in _TOKEN_FIELDS}
    tok_cost = 0.0

    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)

                    # Look for summary entry
                    if entry.get("type") == "summary":
                        summary = entry.get("summary", "")

                    # Track timestamps
                    ts = entry.get("timestamp")
                    if ts:
                        if not start_time or ts < start_time:
                            start_time = ts
                        if not end_time or ts > end_time:
                            end_time = ts

                    # Get project
                    if not project and entry.get("cwd"):
                        project = entry["cwd"]

                    # Accumulate token usage / cost
                    u = extract_usage(entry)
                    if u:
                        for k in _TOKEN_FIELDS:
                            tok_totals[k] += u[k]
                        tok_cost += estimate_cost(
                            u["model"], u["input_tokens"], u["output_tokens"],
                            u["cache_creation_input_tokens"], u["cache_read_input_tokens"])

                except json.JSONDecodeError:
                    continue
    except Exception:
        pass

    # Calculate duration
    duration_mins = None
    if start_time and end_time:
        try:
            start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            duration_mins = round((end_dt - start_dt).total_seconds() / 60, 1)
        except:
            pass

    return {
        "id": session_id,
        "file": str(filepath),
        "summary": summary,
        "start": start_time,
        "end": end_time,
        "duration_mins": duration_mins,
        "project": project,
        "mtime": filepath.stat().st_mtime,
        "total_tokens": sum(tok_totals.values()),
        "cost": tok_cost,
    }


def list_sessions(project_path: Optional[str] = None, fallback_global: bool = True) -> list[dict]:
    """
    List all sessions with their summaries.

    Args:
        project_path: Specific project to search in
        fallback_global: If True and no sessions found locally, search all projects

    Returns list of session info dicts sorted by modification time (newest first).
    """
    projects_dir = find_projects_dir()
    project_dirs = []

    if project_path:
        encoded = encode_project_path(project_path)
        project_dir = projects_dir / encoded
        if not project_dir.exists():
            project_dir = projects_dir / f"-{encoded.lstrip('-')}"
        if project_dir.exists():
            project_dirs = [project_dir]
    else:
        # Use current directory's project
        cwd = Path.cwd()
        encoded = encode_project_path(str(cwd))
        project_dir = projects_dir / encoded
        if not project_dir.exists():
            project_dir = projects_dir / f"-{encoded.lstrip('-')}"
        if project_dir.exists():
            project_dirs = [project_dir]

    sessions = []
    for project_dir in project_dirs:
        # Find main session files (exclude agent-*.jsonl)
        session_files = [f for f in project_dir.glob("*.jsonl")
                        if not f.name.startswith("agent-")]

        for filepath in session_files:
            sessions.append(extract_session_summary(filepath))

    # Fallback: if no sessions found and fallback enabled, search all projects
    if not sessions and fallback_global and not project_path:
        print("No sessions in current directory, searching all projects...", file=sys.stderr)
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            session_files = [f for f in project_dir.glob("*.jsonl")
                            if not f.name.startswith("agent-")]
            for filepath in session_files:
                sessions.append(extract_session_summary(filepath))

    # Sort by modification time (newest first)
    sessions.sort(key=lambda s: s.get("mtime", 0), reverse=True)

    return sessions


# ---------------------------------------------------------------------------
# Token & cost accounting
#
# Estimated USD prices per 1,000,000 tokens. These are ESTIMATES for local
# accounting only. Run /cost in a session, or use the Anthropic Usage & Cost
# API, for authoritative billing. Update these when Anthropic pricing changes.
# Convention (Anthropic prompt caching): cache write = 1.25x input (5m TTL),
# cache read = 0.10x input.
# ---------------------------------------------------------------------------
MODEL_PRICING_PER_MTOK = {
    "opus":   {"input": 5.0,  "output": 25.0},
    "sonnet": {"input": 3.0,  "output": 15.0},
    "haiku":  {"input": 1.0,  "output": 5.0},
    "fable":  {"input": 10.0, "output": 50.0},
    "mythos": {"input": 10.0, "output": 50.0},
}
DEFAULT_PRICING_PER_MTOK = {"input": 5.0, "output": 25.0}


def price_for_model(model: str) -> dict:
    """Match a model id (e.g. 'claude-opus-4-8') to a pricing family by substring."""
    m = (model or "").lower()
    for family, price in MODEL_PRICING_PER_MTOK.items():
        if family in m:
            return price
    return DEFAULT_PRICING_PER_MTOK


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cache_creation_tokens: int, cache_read_tokens: int) -> float:
    """Estimate USD cost for one message's usage. Estimate only — see /cost."""
    p = price_for_model(model)
    return (
        input_tokens * p["input"]
        + output_tokens * p["output"]
        + cache_creation_tokens * p["input"] * 1.25
        + cache_read_tokens * p["input"] * 0.10
    ) / 1_000_000


def extract_usage(entry: dict) -> Optional[dict]:
    """Extract per-message token usage + model from an assistant transcript entry.

    The transcript JSONL is an internal/unstable format; parse defensively and
    return None for anything without a usage block.
    """
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    message = entry.get("message") or {}
    usage = message.get("usage") or entry.get("usage")
    if not isinstance(usage, dict):
        return None
    return {
        "model": message.get("model") or entry.get("model") or "unknown",
        "input_tokens": usage.get("input_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or 0,
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens") or 0,
        "cache_read_input_tokens": usage.get("cache_read_input_tokens") or 0,
    }


_TOKEN_FIELDS = ("input_tokens", "output_tokens",
                 "cache_creation_input_tokens", "cache_read_input_tokens")


def aggregate_tokens(entries: list[dict]) -> dict:
    """Sum token usage across transcript entries → totals, per-model, cost estimate."""
    totals = {k: 0 for k in _TOKEN_FIELDS}
    per_model = {}
    cost = 0.0
    for entry in entries:
        u = extract_usage(entry)
        if not u:
            continue
        for k in _TOKEN_FIELDS:
            totals[k] += u[k]
        pm = per_model.setdefault(u["model"], {**{k: 0 for k in _TOKEN_FIELDS}, "cost": 0.0})
        for k in _TOKEN_FIELDS:
            pm[k] += u[k]
        c = estimate_cost(u["model"], u["input_tokens"], u["output_tokens"],
                          u["cache_creation_input_tokens"], u["cache_read_input_tokens"])
        pm["cost"] += c
        cost += c
    return {
        "totals": totals,
        "total_tokens": sum(totals.values()),
        "per_model": per_model,
        "cost": cost,
    }


def combine_token_aggregates(aggs: list[dict]) -> dict:
    """Merge several aggregate_tokens() results into one session-level total."""
    totals = {k: 0 for k in _TOKEN_FIELDS}
    per_model = {}
    cost = 0.0
    for agg in aggs:
        for k in _TOKEN_FIELDS:
            totals[k] += agg["totals"][k]
        cost += agg["cost"]
        for model, pm in agg["per_model"].items():
            dst = per_model.setdefault(model, {**{k: 0 for k in _TOKEN_FIELDS}, "cost": 0.0})
            for k in _TOKEN_FIELDS:
                dst[k] += pm[k]
            dst["cost"] += pm["cost"]
    return {
        "totals": totals,
        "total_tokens": sum(totals.values()),
        "per_model": per_model,
        "cost": cost,
    }


def parse_jsonl(filepath: Path) -> list[dict]:
    """Parse a JSONL file into list of entries."""
    entries = []
    with open(filepath) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"Warning: Failed to parse line {line_num} in {filepath}: {e}",
                      file=sys.stderr)
    return entries


def extract_all_content(entry: dict) -> list[dict]:
    """Extract all content blocks from a transcript entry."""
    items = []
    message = entry.get("message", {})
    content = message.get("content", [])
    timestamp = entry.get("timestamp", "")
    entry_type = entry.get("type", "")

    if isinstance(content, str):
        # Simple text content
        if content.strip():
            # Use "user" type for user messages to distinguish them
            item_type = "user" if entry_type == "user" else "text"
            items.append({
                "type": item_type,
                "text": content[:1000],
                "timestamp": timestamp,
                "role": entry_type,
            })
        return items

    if not isinstance(content, list):
        return items

    for item in content:
        # Handle string items in content array
        if isinstance(item, str):
            if item.strip():
                content_type = "user" if entry_type == "user" else "text"
                items.append({
                    "type": content_type,
                    "text": item[:1000],
                    "timestamp": timestamp,
                    "role": entry_type,
                })
            continue

        if not isinstance(item, dict):
            continue

        item_type = item.get("type")

        # Also check for text directly in dict without type field
        if not item_type and "text" in item:
            text = item.get("text", "")
            if text.strip():
                content_type = "user" if entry_type == "user" else "text"
                items.append({
                    "type": content_type,
                    "text": text[:1000],
                    "timestamp": timestamp,
                    "role": entry_type,
                })
            continue

        if item_type == "thinking":
            items.append({
                "type": "thinking",
                "text": item.get("thinking", "")[:2000],
                "timestamp": timestamp,
            })
        elif item_type == "text":
            text = item.get("text", "")
            if text.strip():
                # Use "user" type for user messages to distinguish them
                content_type = "user" if entry_type == "user" else "text"
                items.append({
                    "type": content_type,
                    "text": text[:1000],
                    "timestamp": timestamp,
                    "role": entry_type,
                })
        elif item_type == "tool_use":
            items.append({
                "type": "tool_use",
                "id": item.get("id", ""),
                "name": item.get("name", "unknown"),
                "input": item.get("input", {}),
                "timestamp": timestamp,
            })
        elif item_type == "tool_result":
            result_content = item.get("content", "")
            full_length = 0
            if isinstance(result_content, list):
                # Handle complex tool results
                full_length = len(str(result_content))
                result_content = str(result_content)[:500]
            else:
                full_length = len(str(result_content))
                result_content = str(result_content)[:500]
            items.append({
                "type": "tool_result",
                "tool_use_id": item.get("tool_use_id", ""),
                "content": result_content,
                "full_length": full_length,
                "is_error": item.get("is_error", False),
                "timestamp": timestamp,
            })

    return items


def is_warmup_agent(entries: list[dict]) -> bool:
    """Check if this is a warmup agent (should be filtered out)."""
    for entry in entries:
        if entry.get("type") == "user":
            content = entry.get("message", {}).get("content", "")
            if isinstance(content, str):
                # Check for warmup indicators
                if content.strip().lower() in ["warmup", "warmup..."]:
                    return True
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text", "").strip().lower()
                        if text in ["warmup", "warmup..."]:
                            return True
            break  # Only check first user message
    return False


def get_first_user_prompt(entries: list[dict]) -> str:
    """Get the first user prompt for an agent."""
    for entry in entries:
        if entry.get("type") == "user":
            content = entry.get("message", {}).get("content", "")
            if isinstance(content, str):
                if content.strip() and content.strip().lower() not in ["warmup", "warmup..."]:
                    return content[:200]
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, str) and item.strip():
                        return item[:200]
                    if isinstance(item, dict):
                        # Check for text type
                        if item.get("type") == "text":
                            text = item.get("text", "")
                            if text.strip():
                                return text[:200]
                        # Also check for text field without type
                        elif "text" in item and item.get("text", "").strip():
                            return item["text"][:200]
            # Don't break - keep looking for a useful prompt
    return ""


def extract_cloud_metadata(entries: list[dict]) -> dict:
    """
    Extract cloud transmission metadata from session entries.

    Returns stats about tokens, API requests, and what was sent to the cloud.
    """
    cloud_stats = {
        "model": None,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_creation_tokens": 0,
        "total_cache_read_tokens": 0,
        "api_requests": 0,
        "request_ids": set(),
        "message_ids": set(),
        "service_tier": None,
    }

    payload_summary = {
        "files_sent": [],  # List of {path, size_chars, timestamp}
        "user_prompts": [],  # List of {text, full_length, timestamp}
        "total_content_chars": 0,
    }

    seen_files = set()  # Track unique files

    for entry in entries:
        message = entry.get("message", {})
        entry_type = entry.get("type", "")
        timestamp = entry.get("timestamp", "")

        # Extract model and usage from assistant messages
        if entry_type == "assistant" and isinstance(message, dict):
            # Model
            if message.get("model") and not cloud_stats["model"]:
                cloud_stats["model"] = message["model"]

            # Message ID (Anthropic's)
            if message.get("id"):
                cloud_stats["message_ids"].add(message["id"])

            # Usage stats
            usage = message.get("usage", {})
            if usage:
                cloud_stats["total_input_tokens"] += usage.get("input_tokens", 0)
                cloud_stats["total_output_tokens"] += usage.get("output_tokens", 0)
                cloud_stats["total_cache_creation_tokens"] += usage.get("cache_creation_input_tokens", 0)
                cloud_stats["total_cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
                if usage.get("service_tier") and not cloud_stats["service_tier"]:
                    cloud_stats["service_tier"] = usage["service_tier"]

        # Request ID
        if entry.get("requestId"):
            cloud_stats["request_ids"].add(entry["requestId"])

        # Track user prompts sent to cloud
        if entry_type == "user":
            content = message.get("content", "")
            if isinstance(content, str) and content.strip():
                text_preview = content[:200]
                payload_summary["user_prompts"].append({
                    "text": text_preview,
                    "full_length": len(content),
                    "timestamp": timestamp,
                })
                payload_summary["total_content_chars"] += len(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text", "")
                        if text.strip():
                            payload_summary["user_prompts"].append({
                                "text": text[:200],
                                "full_length": len(text),
                                "timestamp": timestamp,
                            })
                            payload_summary["total_content_chars"] += len(text)

        # Track files read (from tool_result for Read tool)
        if entry_type == "user":  # Tool results come as user messages
            content = message.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        result_content = item.get("content", "")
                        # Check if this looks like file content (has line numbers from Read tool)
                        if isinstance(result_content, str) and len(result_content) > 100:
                            payload_summary["total_content_chars"] += len(result_content)

    # Convert sets to lists for JSON serialization
    cloud_stats["api_requests"] = len(cloud_stats["request_ids"])
    cloud_stats["request_ids"] = list(cloud_stats["request_ids"])
    cloud_stats["message_ids"] = list(cloud_stats["message_ids"])

    # Estimated cost via the shared per-model pricing table (estimate — see /cost)
    cloud_stats["estimated_cost_usd"] = round(estimate_cost(
        cloud_stats.get("model", ""),
        cloud_stats["total_input_tokens"],
        cloud_stats["total_output_tokens"],
        cloud_stats["total_cache_creation_tokens"],
        cloud_stats["total_cache_read_tokens"],
    ), 4)

    return {"cloud_stats": cloud_stats, "payload_summary": payload_summary}


def extract_files_from_timeline(timeline: list[dict]) -> list[dict]:
    """
    Extract files that were read and sent to cloud from the timeline.
    """
    files_sent = []
    seen_paths = set()

    for event in timeline:
        if event.get("event") == "tool_use" and event.get("tool_name") == "Read":
            file_path = event.get("tool_input", {}).get("file_path", "")
            if file_path and file_path not in seen_paths:
                seen_paths.add(file_path)
                result = event.get("tool_result", {})
                # Use full_length if available, otherwise fall back to content length
                size = 0
                if result:
                    size = result.get("full_length", 0)
                    if not size:
                        content = result.get("content", "")
                        size = len(content) if isinstance(content, str) else 0
                files_sent.append({
                    "path": file_path,
                    "size_chars": size,
                    "timestamp": event.get("time", ""),
                })

    return files_sent


def build_session_data(main_file: Path, agent_files: list[Path]) -> dict:
    """
    Build unified session data from main and agent transcripts.
    Now extracts full trace: thinking, text, tool_use, tool_result.
    Also extracts cloud transmission metadata.
    """
    main_entries = parse_jsonl(main_file)

    # Extract session metadata
    session_id = None
    start_time = None
    end_time = None
    project = None

    for entry in main_entries:
        if not session_id and entry.get("sessionId"):
            session_id = entry["sessionId"]
        if entry.get("timestamp"):
            ts = entry["timestamp"]
            if not start_time or ts < start_time:
                start_time = ts
            if not end_time or ts > end_time:
                end_time = ts
        if not project and entry.get("cwd"):
            project = entry["cwd"]

    # Extract cloud metadata from main entries
    cloud_data = extract_cloud_metadata(main_entries)

    # Build main agent trace
    main_trace = []
    tool_results = {}  # Map tool_use_id -> result

    for entry in main_entries:
        for item in extract_all_content(entry):
            if item["type"] == "tool_result":
                tool_results[item["tool_use_id"]] = item
            else:
                main_trace.append(item)

    # Attach results to tool calls
    for item in main_trace:
        if item["type"] == "tool_use" and item["id"] in tool_results:
            item["result"] = tool_results[item["id"]]

    main_agent = {
        "id": "main",
        "type": "orchestrator",
        "file": str(main_file),
        "start": start_time,
        "end": end_time,
        "prompt": "",
        "trace": main_trace,
        "tool_calls": [t for t in main_trace if t["type"] == "tool_use"],
        "tokens": aggregate_tokens(main_entries),
    }

    agents = [main_agent]
    skipped_warmup = 0

    # Process agent files
    for agent_file in agent_files:
        agent_entries = parse_jsonl(agent_file)
        if not agent_entries:
            continue

        # Filter out warmup agents
        if is_warmup_agent(agent_entries):
            skipped_warmup += 1
            continue

        agent_id = None
        agent_start = None
        agent_end = None
        agent_type = "subagent"

        for entry in agent_entries:
            if not agent_id and entry.get("agentId"):
                agent_id = entry["agentId"]
            if entry.get("timestamp"):
                ts = entry["timestamp"]
                if not agent_start or ts < agent_start:
                    agent_start = ts
                if not agent_end or ts > agent_end:
                    agent_end = ts

        # Get first user prompt
        first_prompt = get_first_user_prompt(agent_entries)

        # Determine agent type from prompt
        prompt_lower = first_prompt.lower()
        if "explore" in prompt_lower or "codebase" in prompt_lower or "search" in prompt_lower:
            agent_type = "Explore"
        elif "plan" in prompt_lower or "design" in prompt_lower or "architect" in prompt_lower:
            agent_type = "Plan"
        elif "claude-code-guide" in prompt_lower or "documentation" in prompt_lower or "how to" in prompt_lower:
            agent_type = "claude-code-guide"
        elif "general" in prompt_lower or "research" in prompt_lower:
            agent_type = "general-purpose"

        # Build agent trace
        agent_trace = []
        agent_results = {}

        for entry in agent_entries:
            for item in extract_all_content(entry):
                if item["type"] == "tool_result":
                    agent_results[item["tool_use_id"]] = item
                else:
                    agent_trace.append(item)

        # Attach results to tool calls
        for item in agent_trace:
            if item["type"] == "tool_use" and item["id"] in agent_results:
                item["result"] = agent_results[item["id"]]

        agent_data = {
            "id": agent_id or agent_file.stem,
            "type": agent_type,
            "file": str(agent_file),
            "parent": "main",
            "start": agent_start,
            "end": agent_end,
            "prompt": first_prompt,
            "trace": agent_trace,
            "tool_calls": [t for t in agent_trace if t["type"] == "tool_use"],
            "tokens": aggregate_tokens(agent_entries),
        }

        agents.append(agent_data)

    # Build unified timeline from all traces
    timeline = []
    for agent in agents:
        agent_id = agent["id"]
        agent_type = agent["type"]

        for item in agent.get("trace", []):
            event = {
                "time": item.get("timestamp", ""),
                "agent": agent_id,
                "agent_type": agent_type,
                "event": item["type"],
            }

            if item["type"] == "user":
                event["text"] = item["text"]
                event["role"] = "user"
            elif item["type"] == "thinking":
                event["text"] = item["text"]
            elif item["type"] == "text":
                event["text"] = item["text"]
                event["role"] = item.get("role", "")
            elif item["type"] == "tool_use":
                event["tool_name"] = item["name"]
                event["tool_input"] = item["input"]
                event["tool_id"] = item["id"]
                if "result" in item:
                    event["tool_result"] = item["result"]

            timeline.append(event)

    # Integrate hook log events (lifecycle events not in JSONL)
    hook_logs = find_hook_logs(start_time, end_time, project)
    hook_event_count = 0
    if hook_logs:
        main_log, agents_log = hook_logs
        if main_log:
            log_date = date_from_log_filename(main_log)
            hook_events = parse_hook_log(main_log, log_date)
            for he in hook_events:
                # Convert hook event to timeline event format
                event = {
                    "time": he["timestamp"],
                    "agent": "main",  # Hook logs use "orchestrator" but we map to main agent
                    "agent_type": "orchestrator",
                    "source": "hook",
                }

                if he["event_type"] == "PostToolUse":
                    event["event"] = "post_tool_use"
                    event["tool_name"] = he.get("tool_name", "unknown")
                    event["status"] = he.get("status", "success")
                    event["details"] = he.get("details", "")
                else:
                    event["event"] = "lifecycle"
                    event["event_type"] = he["event_type"]
                    event["details"] = he.get("details", "")

                timeline.append(event)
                hook_event_count += 1

            print(f"Integrated {hook_event_count} hook events from {main_log.name}", file=sys.stderr)

    # Sort timeline by timestamp
    timeline.sort(key=lambda x: x.get("time", ""))

    # Calculate duration
    duration_mins = None
    if start_time and end_time:
        try:
            start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            duration_mins = round((end_dt - start_dt).total_seconds() / 60, 1)
        except:
            pass

    # Count events by type
    event_counts = {}
    for e in timeline:
        event_counts[e["event"]] = event_counts.get(e["event"], 0) + 1

    # Extract files sent to cloud from timeline
    files_sent = extract_files_from_timeline(timeline)
    cloud_data["payload_summary"]["files_sent"] = files_sent

    # Also extract cloud metadata from agent files
    for agent_file in agent_files:
        agent_entries = parse_jsonl(agent_file)
        agent_cloud = extract_cloud_metadata(agent_entries)
        # Merge agent cloud stats
        cloud_data["cloud_stats"]["total_input_tokens"] += agent_cloud["cloud_stats"]["total_input_tokens"]
        cloud_data["cloud_stats"]["total_output_tokens"] += agent_cloud["cloud_stats"]["total_output_tokens"]
        cloud_data["cloud_stats"]["total_cache_creation_tokens"] += agent_cloud["cloud_stats"]["total_cache_creation_tokens"]
        cloud_data["cloud_stats"]["total_cache_read_tokens"] += agent_cloud["cloud_stats"]["total_cache_read_tokens"]
        cloud_data["cloud_stats"]["request_ids"].extend(agent_cloud["cloud_stats"]["request_ids"])
        cloud_data["cloud_stats"]["message_ids"].extend(agent_cloud["cloud_stats"]["message_ids"])
        cloud_data["payload_summary"]["total_content_chars"] += agent_cloud["payload_summary"]["total_content_chars"]

    # Recalculate cost with agent data included (per-model pricing)
    cs = cloud_data["cloud_stats"]
    cs["estimated_cost_usd"] = round(estimate_cost(
        cs.get("model", ""),
        cs["total_input_tokens"],
        cs["total_output_tokens"],
        cs["total_cache_creation_tokens"],
        cs["total_cache_read_tokens"],
    ), 4)
    cs["api_requests"] = len(set(cs["request_ids"]))

    return {
        "session": {
            "id": session_id,
            "file": str(main_file),
            "start": start_time,
            "end": end_time,
            "duration_mins": duration_mins,
            "project": project,
        },
        "agents": agents,
        "timeline": timeline,
        "cloud_stats": cloud_data["cloud_stats"],
        "payload_summary": cloud_data["payload_summary"],
        "stats": {
            "total_agents": len(agents),
            "skipped_warmup": skipped_warmup,
            "total_tool_calls": sum(len(a["tool_calls"]) for a in agents),
            "event_counts": event_counts,
            "tools_by_agent": {a["id"]: len(a["tool_calls"]) for a in agents},
            "tokens": combine_token_aggregates([a["tokens"] for a in agents]),
        }
    }


def generate_html(data: dict, all_sessions: list[dict] = None) -> str:
    """Generate HTML visualization from session data with full trace support."""

    # Escape JSON for embedding in script tag
    json_str = json.dumps(data, ensure_ascii=True)
    json_str = json_str.replace("</", "<\\/")

    # Build sessions list for selector
    sessions_json = "[]"
    if all_sessions:
        sessions_json = json.dumps(all_sessions, ensure_ascii=True)
        sessions_json = sessions_json.replace("</", "<\\/")

    session_id = data["session"]["id"] or "Unknown"
    session_id_short = session_id[:8] if session_id != "Unknown" else session_id
    project = data["session"]["project"] or "Unknown"
    duration = data["session"]["duration_mins"] or "?"
    total_agents = data["stats"]["total_agents"]
    total_tools = data["stats"]["total_tool_calls"]
    skipped = data["stats"].get("skipped_warmup", 0)
    event_counts = data["stats"].get("event_counts", {})

    html_template = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Claude Session: {session_id_short}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            padding: 20px;
            line-height: 1.5;
        }}
        .header {{
            background: #16213e;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
        }}
        .header-top {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 10px;
        }}
        .header h1 {{ font-size: 1.5em; margin-bottom: 10px; }}
        .session-selector {{
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 5px;
        }}
        .session-selector select {{
            padding: 8px 12px;
            background: #0f3460;
            border: 1px solid #333;
            color: #eee;
            border-radius: 4px;
            font-size: 14px;
            max-width: 350px;
        }}
        .session-selector .hint {{
            font-size: 0.75em;
            color: #666;
        }}
        .session-cmd {{
            display: none;
            margin-top: 5px;
            padding: 8px 12px;
            background: #0a1628;
            border-radius: 4px;
            font-family: monospace;
            font-size: 0.85em;
            color: #4da8da;
        }}
        .session-cmd.visible {{ display: block; }}
        .header .meta {{ color: #888; font-size: 0.9em; }}
        .header .meta div {{ margin-bottom: 4px; }}
        .stats {{
            display: flex;
            gap: 15px;
            margin-top: 15px;
            flex-wrap: wrap;
        }}
        .stat {{
            background: #0f3460;
            padding: 10px 15px;
            border-radius: 6px;
        }}
        .stat-value {{ font-size: 1.3em; font-weight: bold; color: #e94560; }}
        .stat-label {{ font-size: 0.75em; color: #888; }}
        .cloud-section {{
            background: #0a1628;
            padding: 15px;
            border-radius: 6px;
            margin-top: 15px;
            border-left: 3px solid #4da8da;
        }}
        .cloud-section h3 {{
            color: #4da8da;
            font-size: 0.9em;
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .cloud-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 10px;
        }}
        .cloud-stat {{
            background: #16213e;
            padding: 8px 12px;
            border-radius: 4px;
        }}
        .cloud-stat-value {{
            font-size: 1.1em;
            font-weight: bold;
            color: #4da8da;
        }}
        .cloud-stat-label {{
            font-size: 0.7em;
            color: #666;
        }}
        .cost-highlight {{
            color: #fbbf24;
        }}
        .filter-bar {{
            margin-bottom: 15px;
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
        }}
        .filter-bar select, .filter-bar input, .filter-bar label {{
            padding: 8px 12px;
            background: #16213e;
            border: 1px solid #333;
            color: #eee;
            border-radius: 4px;
            font-size: 14px;
        }}
        .filter-bar label {{
            display: flex;
            align-items: center;
            gap: 6px;
            cursor: pointer;
        }}
        .filter-bar input[type="checkbox"] {{
            width: 16px;
            height: 16px;
        }}
        .tabs {{
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
        }}
        .tab {{
            padding: 10px 20px;
            background: #16213e;
            border: none;
            color: #888;
            cursor: pointer;
            border-radius: 6px;
            font-size: 1em;
        }}
        .tab.active {{ background: #e94560; color: white; }}
        .view {{ display: none; }}
        .view.active {{ display: block; }}

        /* Timeline View */
        .timeline {{ padding: 10px; max-width: 1200px; }}
        .trace-item {{
            margin-bottom: 8px;
            border-radius: 6px;
            overflow: hidden;
        }}
        .trace-header {{
            display: flex;
            align-items: flex-start;
            padding: 10px 12px;
            cursor: pointer;
            gap: 10px;
        }}
        .trace-header:hover {{ filter: brightness(1.1); }}
        .trace-time {{
            width: 75px;
            flex-shrink: 0;
            font-size: 0.75em;
            color: #666;
            font-family: monospace;
        }}
        .trace-agent {{
            width: 90px;
            flex-shrink: 0;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 0.7em;
            text-align: center;
        }}
        .trace-type {{
            width: 70px;
            flex-shrink: 0;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 0.7em;
            text-align: center;
            font-weight: bold;
        }}
        .trace-summary {{
            flex: 1;
            font-size: 0.9em;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .trace-detail {{
            display: none;
            padding: 10px 12px 12px 12px;
            background: rgba(0,0,0,0.2);
            font-size: 0.85em;
            white-space: pre-wrap;
            word-break: break-word;
            max-height: 300px;
            overflow-y: auto;
            font-family: monospace;
            color: #aaa;
        }}
        .trace-item.expanded .trace-detail {{ display: block; }}

        /* Event type colors */
        .trace-thinking {{ background: #1e293b; }}
        .trace-thinking .trace-type {{ background: #6366f1; color: white; }}
        .trace-text {{ background: #1a2e1a; }}
        .trace-text .trace-type {{ background: #22c55e; color: white; }}
        .trace-user {{ background: #1e3a5f; }}
        .trace-user .trace-type {{ background: #3b82f6; color: white; }}
        .trace-tool_use {{ background: #16213e; }}
        .trace-tool_use .trace-type {{ background: #e94560; color: white; }}

        /* Lifecycle events (Stop, SessionStart, etc.) */
        .trace-lifecycle {{ background: #2d1f3d; }}
        .trace-lifecycle .trace-type {{ background: #9333ea; color: white; }}

        /* PostToolUse success */
        .trace-post_tool_use {{ background: #1a2e1a; }}
        .trace-post_tool_use .trace-type {{ background: #22c55e; color: white; }}

        /* PostToolUse error */
        .trace-post_tool_use.error {{ background: #2e1a1a; }}
        .trace-post_tool_use.error .trace-type {{ background: #ef4444; color: white; }}

        /* Agent colors */
        .agent-orchestrator {{ background: #0f3460; color: #4da8da; }}
        .agent-Explore {{ background: #1a472a; color: #4ade80; }}
        .agent-Plan {{ background: #4a3728; color: #fbbf24; }}
        .agent-subagent {{ background: #3d2952; color: #a78bfa; }}
        .agent-claude-code-guide {{ background: #2d3748; color: #68d391; }}
        .agent-general-purpose {{ background: #374151; color: #9ca3af; }}

        /* Tool result */
        .tool-result {{
            margin-top: 8px;
            padding: 8px;
            background: #0a1628;
            border-radius: 4px;
            border-left: 3px solid #22c55e;
        }}
        .tool-result.error {{ border-left-color: #ef4444; }}
        .tool-result-label {{ font-size: 0.75em; color: #666; margin-bottom: 4px; }}

        /* Tree View */
        .tree {{ padding: 10px; }}
        .tree-agent {{
            margin-bottom: 15px;
            background: #16213e;
            border-radius: 8px;
            overflow: hidden;
        }}
        .tree-agent-header {{
            padding: 12px 15px;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .tree-agent-header:hover {{ background: #1e2a4a; }}
        .tree-agent-name {{ font-weight: bold; }}
        .tree-agent-prompt {{
            font-size: 0.8em;
            color: #888;
            margin-top: 4px;
            font-style: italic;
        }}
        .tree-agent-stats {{ font-size: 0.85em; color: #888; }}
        .tree-agent-trace {{
            padding: 0 15px 15px 15px;
            display: none;
            max-height: 500px;
            overflow-y: auto;
        }}
        .tree-agent.expanded .tree-agent-trace {{ display: block; }}
        .tree-item {{
            padding: 6px 10px;
            margin-top: 4px;
            border-radius: 4px;
            font-size: 0.85em;
        }}
        .tree-item-user {{ background: #1e3a5f; border-left: 3px solid #3b82f6; }}
        .tree-item-thinking {{ background: #1e293b; border-left: 3px solid #6366f1; }}
        .tree-item-text {{ background: #1a2e1a; border-left: 3px solid #22c55e; }}
        .tree-item-tool {{ background: #0f3460; border-left: 3px solid #e94560; }}
        .tree-item-time {{ color: #666; font-family: monospace; font-size: 0.8em; }}
        .tree-item-label {{ font-weight: bold; margin-left: 8px; }}
        .tree-item-content {{ margin-top: 4px; color: #aaa; font-size: 0.9em; }}
        .expand-icon {{ transition: transform 0.2s; display: inline-block; margin-right: 8px; }}
        .tree-agent.expanded .expand-icon {{ transform: rotate(90deg); }}

        /* Payload View */
        .payload {{ padding: 10px; max-width: 1200px; }}
        .payload-section {{
            background: #16213e;
            border-radius: 8px;
            margin-bottom: 15px;
            overflow: hidden;
        }}
        .payload-section-header {{
            padding: 12px 15px;
            background: #0f3460;
            font-weight: bold;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .payload-section-content {{
            padding: 15px;
        }}
        .payload-file {{
            display: flex;
            justify-content: space-between;
            padding: 8px 12px;
            background: #0a1628;
            border-radius: 4px;
            margin-bottom: 6px;
            font-family: monospace;
            font-size: 0.85em;
        }}
        .payload-file-path {{ color: #4da8da; word-break: break-all; }}
        .payload-file-size {{ color: #888; white-space: nowrap; margin-left: 10px; }}
        .payload-prompt {{
            padding: 10px 12px;
            background: #1e3a5f;
            border-radius: 4px;
            margin-bottom: 8px;
            border-left: 3px solid #3b82f6;
        }}
        .payload-prompt-text {{
            color: #ddd;
            font-size: 0.9em;
            white-space: pre-wrap;
            word-break: break-word;
        }}
        .payload-prompt-meta {{
            display: flex;
            justify-content: space-between;
            font-size: 0.75em;
            color: #666;
            margin-top: 6px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <div class="header-top">
            <div>
                <h1>Agent Trace Viewer</h1>
                <div class="meta">
                    <div>Project: {project}</div>
                    <div>Duration: {duration} minutes</div>
                </div>
            </div>
            <div class="session-selector">
                <select id="sessionSelect">
                    <option value="{session_id}">{session_id_short} - (current)</option>
                </select>
                <div class="hint">Select a session to view</div>
                <div id="sessionCmd" class="session-cmd"></div>
            </div>
        </div>
        <div class="stats">
            <div class="stat">
                <div class="stat-value">{total_agents}</div>
                <div class="stat-label">Agents</div>
            </div>
            <div class="stat">
                <div class="stat-value">{total_tools}</div>
                <div class="stat-label">Tool Calls</div>
            </div>
            <div class="stat">
                <div class="stat-value">{user_count}</div>
                <div class="stat-label">User Prompts</div>
            </div>
            <div class="stat">
                <div class="stat-value">{thinking_count}</div>
                <div class="stat-label">Thinking</div>
            </div>
            <div class="stat">
                <div class="stat-value">{text_count}</div>
                <div class="stat-label">Responses</div>
            </div>
            <div class="stat">
                <div class="stat-value">{lifecycle_count}</div>
                <div class="stat-label">Lifecycle</div>
            </div>
            <div class="stat">
                <div class="stat-value">{total_tokens_fmt}</div>
                <div class="stat-label">Tokens (est)</div>
            </div>
            <div class="stat">
                <div class="stat-value">{est_cost_fmt}</div>
                <div class="stat-label">Est. cost</div>
            </div>
        </div>

        <div class="cloud-section">
            <h3>Cloud Transmission</h3>
            <div class="cloud-grid">
                <div class="cloud-stat">
                    <div class="cloud-stat-value">{model_short}</div>
                    <div class="cloud-stat-label">Model</div>
                </div>
                <div class="cloud-stat">
                    <div class="cloud-stat-value">{api_requests}</div>
                    <div class="cloud-stat-label">API Requests</div>
                </div>
                <div class="cloud-stat">
                    <div class="cloud-stat-value">{input_tokens_fmt}</div>
                    <div class="cloud-stat-label">Input Tokens</div>
                </div>
                <div class="cloud-stat">
                    <div class="cloud-stat-value">{output_tokens_fmt}</div>
                    <div class="cloud-stat-label">Output Tokens</div>
                </div>
                <div class="cloud-stat">
                    <div class="cloud-stat-value">{cache_read_fmt}</div>
                    <div class="cloud-stat-label">Cache Read</div>
                </div>
                <div class="cloud-stat">
                    <div class="cloud-stat-value">{cache_create_fmt}</div>
                    <div class="cloud-stat-label">Cache Create</div>
                </div>
                <div class="cloud-stat">
                    <div class="cloud-stat-value cost-highlight">${estimated_cost}</div>
                    <div class="cloud-stat-label">Est. Cost</div>
                </div>
                <div class="cloud-stat">
                    <div class="cloud-stat-value">{files_sent_count}</div>
                    <div class="cloud-stat-label">Files Sent</div>
                </div>
            </div>
        </div>
    </div>

    <div class="filter-bar">
        <select id="agentFilter">
            <option value="">All Agents</option>
        </select>
        <select id="eventFilter">
            <option value="">All Events</option>
            <option value="user">User Prompt</option>
            <option value="thinking">Thinking</option>
            <option value="tool_use">Tool Use</option>
            <option value="text">Text Response</option>
            <option value="lifecycle">Lifecycle (Stop, SessionStart...)</option>
            <option value="post_tool_use">PostToolUse</option>
        </select>
        <input type="text" id="searchInput" placeholder="Search...">
        <label><input type="checkbox" id="showUser" checked> User</label>
        <label><input type="checkbox" id="showThinking" checked> Thinking</label>
        <label><input type="checkbox" id="showText" checked> Responses</label>
        <label><input type="checkbox" id="showTools" checked> Tools</label>
        <label><input type="checkbox" id="showLifecycle" checked> Lifecycle</label>
    </div>

    <div class="tabs">
        <button class="tab active" data-view="timeline">Timeline</button>
        <button class="tab" data-view="tree">Tree</button>
        <button class="tab" data-view="payload">Payload</button>
    </div>

    <div id="timeline" class="view active">
        <div class="timeline"></div>
    </div>

    <div id="tree" class="view">
        <div class="tree"></div>
    </div>

    <div id="payload" class="view">
        <div class="payload"></div>
    </div>

    <script>
        const data = {json_str};
        const allSessions = {sessions_json};
        const currentSessionId = '{session_id}';

        // Populate session selector
        const sessionSelect = document.getElementById('sessionSelect');
        const sessionCmd = document.getElementById('sessionCmd');

        if (allSessions && allSessions.length > 0) {{
            // Clear default option
            sessionSelect.innerHTML = '';

            allSessions.forEach(s => {{
                const opt = document.createElement('option');
                opt.value = s.id;
                const shortId = s.id.substring(0, 8);
                const summary = s.summary ? s.summary.substring(0, 40) : '(no summary)';
                const duration = s.duration_mins ? s.duration_mins + 'm' : '?';
                opt.textContent = shortId + ' - ' + duration + ' - ' + summary;
                if (s.id === currentSessionId) {{
                    opt.selected = true;
                    opt.textContent += ' (current)';
                }}
                sessionSelect.appendChild(opt);
            }});
        }}

        sessionSelect.addEventListener('change', function() {{
            const selectedId = this.value;
            if (selectedId !== currentSessionId) {{
                sessionCmd.textContent = 'Run: claude-session-analyzer ' + selectedId.substring(0, 8) + ' --open';
                sessionCmd.classList.add('visible');
            }} else {{
                sessionCmd.classList.remove('visible');
            }}
        }});

        // Populate agent filter
        const agentFilter = document.getElementById('agentFilter');
        data.agents.forEach(a => {{
            const opt = document.createElement('option');
            opt.value = a.id;
            opt.textContent = a.type + ' (' + a.id.substring(0, 8) + ')';
            agentFilter.appendChild(opt);
        }});

        function formatTime(ts) {{
            if (!ts) return '';
            const d = new Date(ts);
            return d.toLocaleTimeString();
        }}

        function escapeHtml(str) {{
            if (!str) return '';
            return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }}

        function formatInput(input) {{
            if (!input) return '';
            if (input.file_path) return input.file_path;
            if (input.command) return input.command.substring(0, 100);
            if (input.pattern) return '"' + input.pattern + '"';
            if (input.query) return input.query;
            if (input.url) return input.url;
            if (input.description) return input.description;
            return JSON.stringify(input).substring(0, 100);
        }}

        function getAgentClass(type) {{
            const classes = {{
                'orchestrator': 'agent-orchestrator',
                'Explore': 'agent-Explore',
                'Plan': 'agent-Plan',
                'claude-code-guide': 'agent-claude-code-guide',
                'general-purpose': 'agent-general-purpose',
            }};
            return classes[type] || 'agent-subagent';
        }}

        function getSummary(e) {{
            if (e.event === 'user') return escapeHtml((e.text || '').substring(0, 100));
            if (e.event === 'thinking') return escapeHtml((e.text || '').substring(0, 100));
            if (e.event === 'text') return escapeHtml((e.text || '').substring(0, 100));
            if (e.event === 'tool_use') return escapeHtml(e.tool_name + ': ' + formatInput(e.tool_input));
            if (e.event === 'lifecycle') return escapeHtml(e.event_type + ': ' + (e.details || ''));
            if (e.event === 'post_tool_use') return escapeHtml(e.tool_name + ': ' + e.status);
            return '';
        }}

        function getDetail(e) {{
            let detail = '';
            if (e.event === 'user') {{
                detail = escapeHtml(e.text || '');
            }} else if (e.event === 'thinking') {{
                detail = escapeHtml(e.text || '');
            }} else if (e.event === 'text') {{
                detail = escapeHtml(e.text || '');
            }} else if (e.event === 'tool_use') {{
                detail = 'Input:\\n' + escapeHtml(JSON.stringify(e.tool_input, null, 2));
                if (e.tool_result) {{
                    const isError = e.tool_result.is_error;
                    detail += '\\n\\n<div class="tool-result' + (isError ? ' error' : '') + '">';
                    detail += '<div class="tool-result-label">' + (isError ? 'Error:' : 'Result:') + '</div>';
                    detail += escapeHtml(e.tool_result.content || '');
                    detail += '</div>';
                }}
            }} else if (e.event === 'lifecycle') {{
                detail = 'Event: ' + escapeHtml(e.event_type) + '\\nDetails: ' + escapeHtml(e.details || '(none)');
            }} else if (e.event === 'post_tool_use') {{
                detail = 'Tool: ' + escapeHtml(e.tool_name) + '\\nStatus: ' + escapeHtml(e.status) + '\\nDetails: ' + escapeHtml(e.details || '(none)');
            }}
            return detail;
        }}

        function renderTimeline() {{
            const container = document.querySelector('.timeline');
            const agentVal = agentFilter.value;
            const eventVal = document.getElementById('eventFilter').value;
            const search = document.getElementById('searchInput').value.toLowerCase();
            const showUser = document.getElementById('showUser').checked;
            const showThinking = document.getElementById('showThinking').checked;
            const showText = document.getElementById('showText').checked;
            const showTools = document.getElementById('showTools').checked;
            const showLifecycle = document.getElementById('showLifecycle').checked;

            let filtered = data.timeline.filter(e => {{
                if (e.event === 'user' && !showUser) return false;
                if (e.event === 'thinking' && !showThinking) return false;
                if (e.event === 'text' && !showText) return false;
                if (e.event === 'tool_use' && !showTools) return false;
                if ((e.event === 'lifecycle' || e.event === 'post_tool_use') && !showLifecycle) return false;
                return true;
            }});

            if (agentVal) filtered = filtered.filter(e => e.agent === agentVal);
            if (eventVal) filtered = filtered.filter(e => e.event === eventVal);
            if (search) {{
                filtered = filtered.filter(e => {{
                    const summary = getSummary(e).toLowerCase();
                    return summary.includes(search);
                }});
            }}

            let html = '';
            filtered.forEach((e, idx) => {{
                let eventClass = 'trace-' + e.event;
                // Add error class for post_tool_use with error status
                if (e.event === 'post_tool_use' && e.status === 'error') {{
                    eventClass += ' error';
                }}
                html += '<div class="trace-item ' + eventClass + '" data-idx="' + idx + '">';
                html += '<div class="trace-header">';
                html += '<div class="trace-time">' + formatTime(e.time) + '</div>';
                html += '<div class="trace-agent ' + getAgentClass(e.agent_type) + '">' + escapeHtml(e.agent_type) + '</div>';
                html += '<div class="trace-type">' + e.event.replace('_', ' ') + '</div>';
                html += '<div class="trace-summary">' + getSummary(e) + '</div>';
                html += '</div>';
                html += '<div class="trace-detail">' + getDetail(e) + '</div>';
                html += '</div>';
            }});

            container.innerHTML = html || '<p style="color:#666;padding:20px;">No events match the filter</p>';

            // Add click handlers for expand/collapse
            container.querySelectorAll('.trace-header').forEach(header => {{
                header.addEventListener('click', function() {{
                    this.parentElement.classList.toggle('expanded');
                }});
            }});
        }}

        function renderTree() {{
            const container = document.querySelector('.tree');
            let html = '';

            data.agents.forEach(agent => {{
                const traceCount = (agent.trace || []).length;
                html += '<div class="tree-agent">';
                html += '<div class="tree-agent-header ' + getAgentClass(agent.type) + '">';
                html += '<div>';
                html += '<span class="tree-agent-name"><span class="expand-icon">▶</span>' + escapeHtml(agent.type) + ' (' + agent.id.substring(0, 8) + ')</span>';
                if (agent.prompt) {{
                    html += '<div class="tree-agent-prompt">"' + escapeHtml(agent.prompt.substring(0, 80)) + '..."</div>';
                }}
                html += '</div>';
                html += '<span class="tree-agent-stats">' + traceCount + ' events, ' + agent.tool_calls.length + ' tools</span>';
                html += '</div>';
                html += '<div class="tree-agent-trace">';

                (agent.trace || []).forEach(item => {{
                    if (item.type === 'user') {{
                        html += '<div class="tree-item tree-item-user">';
                        html += '<span class="tree-item-time">' + formatTime(item.timestamp) + '</span>';
                        html += '<span class="tree-item-label">USER</span>';
                        html += '<div class="tree-item-content">' + escapeHtml((item.text || '').substring(0, 200)) + '...</div>';
                        html += '</div>';
                    }} else if (item.type === 'thinking') {{
                        html += '<div class="tree-item tree-item-thinking">';
                        html += '<span class="tree-item-time">' + formatTime(item.timestamp) + '</span>';
                        html += '<span class="tree-item-label">THINKING</span>';
                        html += '<div class="tree-item-content">' + escapeHtml((item.text || '').substring(0, 200)) + '...</div>';
                        html += '</div>';
                    }} else if (item.type === 'text') {{
                        html += '<div class="tree-item tree-item-text">';
                        html += '<span class="tree-item-time">' + formatTime(item.timestamp) + '</span>';
                        html += '<span class="tree-item-label">RESPONSE</span>';
                        html += '<div class="tree-item-content">' + escapeHtml((item.text || '').substring(0, 200)) + '...</div>';
                        html += '</div>';
                    }} else if (item.type === 'tool_use') {{
                        html += '<div class="tree-item tree-item-tool">';
                        html += '<span class="tree-item-time">' + formatTime(item.timestamp) + '</span>';
                        html += '<span class="tree-item-label">' + escapeHtml(item.name) + '</span>';
                        html += '<div class="tree-item-content">' + escapeHtml(formatInput(item.input)) + '</div>';
                        if (item.result) {{
                            html += '<div class="tool-result' + (item.result.is_error ? ' error' : '') + '">';
                            html += '<div class="tool-result-label">Result:</div>';
                            html += escapeHtml((item.result.content || '').substring(0, 200));
                            html += '</div>';
                        }}
                        html += '</div>';
                    }}
                }});

                html += '</div></div>';
            }});

            container.innerHTML = html;

            // Toggle expand
            container.querySelectorAll('.tree-agent-header').forEach(header => {{
                header.addEventListener('click', function() {{
                    this.parentElement.classList.toggle('expanded');
                }});
            }});
        }}

        function renderPayload() {{
            const container = document.querySelector('.payload');
            const payload = data.payload_summary || {{}};
            const filesSent = payload.files_sent || [];
            const userPrompts = payload.user_prompts || [];

            let html = '';

            // Files section
            html += '<div class="payload-section">';
            html += '<div class="payload-section-header">';
            html += '<span>Files Sent to Cloud (' + filesSent.length + ')</span>';
            html += '<span style="color:#888;font-size:0.85em;">Content read and transmitted</span>';
            html += '</div>';
            html += '<div class="payload-section-content">';

            if (filesSent.length === 0) {{
                html += '<p style="color:#666;">No files read during this session</p>';
            }} else {{
                filesSent.forEach(file => {{
                    const sizeKb = (file.size_chars / 1024).toFixed(1);
                    html += '<div class="payload-file">';
                    html += '<span class="payload-file-path">' + escapeHtml(file.path) + '</span>';
                    html += '<span class="payload-file-size">' + file.size_chars.toLocaleString() + ' chars (' + sizeKb + ' KB) @ ' + formatTime(file.timestamp) + '</span>';
                    html += '</div>';
                }});
            }}
            html += '</div></div>';

            // User prompts section
            html += '<div class="payload-section">';
            html += '<div class="payload-section-header">';
            html += '<span>User Prompts (' + userPrompts.length + ')</span>';
            html += '<span style="color:#888;font-size:0.85em;">Your messages sent to API</span>';
            html += '</div>';
            html += '<div class="payload-section-content">';

            if (userPrompts.length === 0) {{
                html += '<p style="color:#666;">No user prompts found</p>';
            }} else {{
                userPrompts.forEach((prompt, idx) => {{
                    html += '<div class="payload-prompt">';
                    html += '<div class="payload-prompt-text">' + escapeHtml(prompt.text) + (prompt.full_length > 200 ? '...' : '') + '</div>';
                    html += '<div class="payload-prompt-meta">';
                    html += '<span>#' + (idx + 1) + ' - ' + prompt.full_length.toLocaleString() + ' chars</span>';
                    html += '<span>' + formatTime(prompt.timestamp) + '</span>';
                    html += '</div></div>';
                }});
            }}
            html += '</div></div>';

            // Summary
            const totalChars = payload.total_content_chars || 0;
            const totalKb = (totalChars / 1024).toFixed(1);
            html += '<div class="payload-section">';
            html += '<div class="payload-section-header">';
            html += '<span>Transmission Summary</span>';
            html += '</div>';
            html += '<div class="payload-section-content" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;">';
            html += '<div class="cloud-stat"><div class="cloud-stat-value">' + totalChars.toLocaleString() + '</div><div class="cloud-stat-label">Total Characters</div></div>';
            html += '<div class="cloud-stat"><div class="cloud-stat-value">' + totalKb + ' KB</div><div class="cloud-stat-label">Approx Size</div></div>';
            html += '<div class="cloud-stat"><div class="cloud-stat-value">' + filesSent.length + '</div><div class="cloud-stat-label">Files Read</div></div>';
            html += '<div class="cloud-stat"><div class="cloud-stat-value">' + userPrompts.length + '</div><div class="cloud-stat-label">User Messages</div></div>';
            html += '</div></div>';

            container.innerHTML = html;
        }}

        // Tab switching
        document.querySelectorAll('.tab').forEach(tab => {{
            tab.addEventListener('click', function() {{
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
                this.classList.add('active');
                document.getElementById(this.dataset.view).classList.add('active');
            }});
        }});

        // Filter events
        agentFilter.addEventListener('change', renderTimeline);
        document.getElementById('eventFilter').addEventListener('change', renderTimeline);
        document.getElementById('searchInput').addEventListener('input', renderTimeline);
        document.getElementById('showUser').addEventListener('change', renderTimeline);
        document.getElementById('showThinking').addEventListener('change', renderTimeline);
        document.getElementById('showText').addEventListener('change', renderTimeline);
        document.getElementById('showTools').addEventListener('change', renderTimeline);
        document.getElementById('showLifecycle').addEventListener('change', renderTimeline);

        // Initial render
        renderTimeline();
        renderTree();
        renderPayload();
    </script>
</body>
</html>'''

    # Calculate lifecycle count (lifecycle + post_tool_use events)
    lifecycle_count = event_counts.get("lifecycle", 0) + event_counts.get("post_tool_use", 0)

    # Extract cloud stats (Payload tab: what was transmitted to the API)
    cloud_stats = data.get("cloud_stats", {})
    payload_summary = data.get("payload_summary", {})

    # Format token numbers for display
    def format_tokens(n):
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        elif n >= 1_000:
            return f"{n/1_000:.1f}K"
        return str(n)

    # Get model short name
    model = cloud_stats.get("model", "unknown") or "unknown"
    model_short = model.split("-")[-1] if model != "unknown" else "N/A"
    if "opus" in model.lower():
        model_short = "Opus"
    elif "sonnet" in model.lower():
        model_short = "Sonnet"
    elif "haiku" in model.lower():
        model_short = "Haiku"

    # Token & cost summary (estimate — per-model pricing, see estimate_cost)
    tokens_stats = data["stats"].get("tokens") or {}
    total_tokens_val = tokens_stats.get("total_tokens", 0)
    total_tokens_fmt = f"{total_tokens_val / 1000:.1f}K" if total_tokens_val else "0"
    est_cost_fmt = f"${tokens_stats.get('cost', 0):.2f}"

    return html_template.format(
        session_id_short=session_id_short,
        session_id=session_id,
        project=project,
        duration=duration,
        total_agents=total_agents,
        total_tools=total_tools,
        user_count=event_counts.get("user", 0),
        thinking_count=event_counts.get("thinking", 0),
        text_count=event_counts.get("text", 0),
        lifecycle_count=lifecycle_count,
        total_tokens_fmt=total_tokens_fmt,
        est_cost_fmt=est_cost_fmt,
        json_str=json_str,
        sessions_json=sessions_json,
        model_short=model_short,
        api_requests=cloud_stats.get("api_requests", 0),
        input_tokens_fmt=format_tokens(cloud_stats.get("total_input_tokens", 0)),
        output_tokens_fmt=format_tokens(cloud_stats.get("total_output_tokens", 0)),
        cache_read_fmt=format_tokens(cloud_stats.get("total_cache_read_tokens", 0)),
        cache_create_fmt=format_tokens(cloud_stats.get("total_cache_creation_tokens", 0)),
        estimated_cost=f"{cloud_stats.get('estimated_cost_usd', 0):.2f}",
        files_sent_count=len(payload_summary.get("files_sent", [])),
    )


def generate_digest(data: dict) -> str:
    """Generate a concise markdown digest for feeding into a new Claude session."""
    lines = []

    session = data["session"]
    stats = data["stats"]
    timeline = data["timeline"]
    agents = data["agents"]

    # Header
    session_id = session.get("id", "unknown")[:8]
    lines.append(f"# Session Digest: {session_id}")
    lines.append("")
    lines.append(f"**Project:** {session.get('project', 'unknown')}")
    lines.append(f"**Duration:** {session.get('duration_mins', '?')} minutes")
    lines.append(f"**Agents:** {stats.get('total_agents', 0)} ({', '.join(a['type'] for a in agents[:5])})")
    lines.append("")

    # Cloud transmission summary (payload / what was sent to the API)
    cloud_stats = data.get("cloud_stats", {})
    payload_summary = data.get("payload_summary", {})
    if cloud_stats.get("model"):
        lines.append("## Cloud Transmission Summary")
        lines.append("")
        lines.append(f"- **Model:** {cloud_stats.get('model', 'unknown')}")
        lines.append(f"- **API Requests:** {cloud_stats.get('api_requests', 0)}")
        total_input = cloud_stats.get('total_input_tokens', 0)
        total_output = cloud_stats.get('total_output_tokens', 0)
        lines.append(f"- **Total Tokens:** {total_input:,} input / {total_output:,} output")
        lines.append(f"- **Estimated Cost:** ${cloud_stats.get('estimated_cost_usd', 0):.2f}")
        files_sent = payload_summary.get("files_sent", [])
        total_chars = payload_summary.get("total_content_chars", 0)
        lines.append(f"- **Files Sent:** {len(files_sent)} files ({total_chars:,} chars)")
        lines.append("")

    # Token & cost accounting (estimate — run /cost for authoritative billing)
    tokens = stats.get("tokens") or {}
    if tokens.get("total_tokens"):
        t = tokens["totals"]
        lines.append("## Token & Cost (estimate)")
        lines.append("")
        lines.append(
            f"**Total tokens:** {tokens['total_tokens']:,} "
            f"(in {t['input_tokens']:,} · out {t['output_tokens']:,} · "
            f"cache-write {t['cache_creation_input_tokens']:,} · "
            f"cache-read {t['cache_read_input_tokens']:,})")
        lines.append(
            f"**Estimated cost:** ${tokens['cost']:.4f} "
            f"(estimate — run /cost or the Usage & Cost API for authoritative billing)")
        if tokens.get("per_model"):
            parts = []
            for model, pm in sorted(tokens["per_model"].items(),
                                    key=lambda kv: kv[1]["cost"], reverse=True):
                mt = sum(pm[k] for k in _TOKEN_FIELDS)
                parts.append(f"{model} ({mt:,} tok, ${pm['cost']:.4f})")
            lines.append(f"**By model:** {', '.join(parts)}")
        agent_lines = []
        for a in agents:
            atok = a.get("tokens") or {}
            if atok.get("total_tokens"):
                label = "orchestrator" if a["id"] == "main" else f"{a.get('type', '')}:{a['id'][:8]}"
                agent_lines.append(
                    f"- {label}: {atok['total_tokens']:,} tok, ${atok['cost']:.4f}")
        if len(agent_lines) > 1:
            lines.append("")
            lines.append("**By agent:**")
            lines.extend(agent_lines)
        lines.append("")

    # User prompts
    user_prompts = [e for e in timeline if e.get("event") == "user" and e.get("text")]
    if user_prompts:
        lines.append("## User Prompts (chronological)")
        lines.append("")
        for i, prompt in enumerate(user_prompts[:10], 1):  # Limit to first 10
            text = prompt.get("text", "")[:300]
            text = text.replace("\n", " ").strip()
            if len(prompt.get("text", "")) > 300:
                text += "..."
            lines.append(f"{i}. \"{text}\"")
        if len(user_prompts) > 10:
            lines.append(f"   ... and {len(user_prompts) - 10} more prompts")
        lines.append("")

    # Key thinking (extract most substantive ones)
    thinking_blocks = [e for e in timeline if e.get("event") == "thinking" and e.get("text")]
    if thinking_blocks:
        lines.append("## Key Thinking/Decisions")
        lines.append("")
        # Get longer thinking blocks (more likely to be substantive)
        substantive = sorted(thinking_blocks, key=lambda x: len(x.get("text", "")), reverse=True)[:5]
        for block in substantive:
            text = block.get("text", "")[:400]
            # Get first meaningful sentence/thought
            text = text.replace("\n", " ").strip()
            if len(block.get("text", "")) > 400:
                text += "..."
            lines.append(f"- {text}")
        lines.append("")

    # Commands run
    bash_commands = [e for e in timeline if e.get("event") == "tool_use" and e.get("tool_name") == "Bash"]
    if bash_commands:
        lines.append("## Commands Run")
        lines.append("")
        seen = set()
        for cmd in bash_commands:
            command = cmd.get("tool_input", {}).get("command", "")[:100]
            if command and command not in seen:
                seen.add(command)
                lines.append(f"- `{command}`")
        lines.append("")

    # Files modified
    file_ops = [e for e in timeline if e.get("event") == "tool_use" and e.get("tool_name") in ["Edit", "Write"]]
    if file_ops:
        lines.append("## Files Modified")
        lines.append("")
        seen = set()
        for op in file_ops:
            tool = op.get("tool_name", "")
            filepath = op.get("tool_input", {}).get("file_path", "")
            if filepath and filepath not in seen:
                seen.add(filepath)
                # Shorten path for display
                if len(filepath) > 60:
                    filepath = "..." + filepath[-57:]
                lines.append(f"- {filepath} ({tool})")
        lines.append("")

    # Errors encountered
    errors = [e for e in timeline if e.get("event") == "tool_use" and e.get("tool_result", {}).get("is_error")]
    if errors:
        lines.append("## Errors Encountered")
        lines.append("")
        for err in errors[:5]:  # Limit to 5
            tool = err.get("tool_name", "unknown")
            error_content = err.get("tool_result", {}).get("content", "")[:200]
            error_content = error_content.replace("\n", " ").strip()
            lines.append(f"- **{tool}**: {error_content}")
        lines.append("")

    # Where it left off
    lines.append("## Where It Left Off")
    lines.append("")

    # Last user prompt
    if user_prompts:
        last_prompt = user_prompts[-1].get("text", "")[:500]
        lines.append("**Last user request:**")
        lines.append(f"> {last_prompt}")
        lines.append("")

    # Last assistant response
    text_responses = [e for e in timeline if e.get("event") == "text" and e.get("text")]
    if text_responses:
        last_response = text_responses[-1].get("text", "")[:500]
        last_response = last_response.replace("\n", "\n> ")
        lines.append("**Last assistant response:**")
        lines.append(f"> {last_response}")
        lines.append("")

    # Pending work (from last thinking)
    if thinking_blocks:
        last_thinking = thinking_blocks[-1].get("text", "")[:300]
        if last_thinking:
            lines.append("**Last thinking:**")
            lines.append(f"> {last_thinking}")
            lines.append("")

    return "\n".join(lines)


def format_session_list(sessions: list[dict]) -> str:
    """Format session list for terminal display."""
    if not sessions:
        return "No sessions found."

    lines = []
    lines.append(f"{'ID':<12} {'Dur':>6} {'Tokens':>9} {'Est.$':>8}  {'Project':<26} {'Summary'}")
    lines.append("-" * 110)

    for s in sessions:
        sid = s["id"][:8]  # Short ID is enough to identify
        duration = f"{s['duration_mins']}m" if s["duration_mins"] else "?"
        total_tokens = s.get("total_tokens", 0) or 0
        tok_str = f"{total_tokens / 1000:.1f}K" if total_tokens else "-"
        cost = s.get("cost", 0) or 0
        cost_str = f"${cost:.2f}" if cost else "-"
        project = s.get("project", "")
        # Shorten project path
        if project:
            project = project.replace(str(Path.home()), "~")
            if len(project) > 24:
                project = "..." + project[-21:]
        else:
            project = "(unknown)"
        summary = s["summary"][:40] + "..." if len(s["summary"]) > 40 else s["summary"]
        if not summary:
            summary = "(no summary)"
        lines.append(f"{sid:<12} {duration:>6} {tok_str:>9} {cost_str:>8}  {project:<26} {summary}")

    lines.append("")
    lines.append("Tokens/Est.$ are the orchestrator transcript only (estimate). "
                 "Run `--digest` for the full per-agent breakdown, or /cost for authoritative billing.")
    return "\n".join(lines)


def format_cloud_stats(data: dict) -> str:
    """Format cloud transmission stats for terminal display."""
    session = data.get("session", {})
    cloud_stats = data.get("cloud_stats", {})

    session_id = session.get("id", "unknown")[:8]
    model = cloud_stats.get("model", "unknown") or "unknown"

    lines = []
    lines.append(f"Session: {session_id}")
    lines.append(f"Model: {model}")
    lines.append(f"API Requests: {cloud_stats.get('api_requests', 0)}")
    lines.append("")
    lines.append("Tokens:")
    lines.append(f"  Input: {cloud_stats.get('total_input_tokens', 0):,}")
    lines.append(f"  Output: {cloud_stats.get('total_output_tokens', 0):,}")
    lines.append(f"  Cache Creation: {cloud_stats.get('total_cache_creation_tokens', 0):,}")
    lines.append(f"  Cache Read: {cloud_stats.get('total_cache_read_tokens', 0):,}")
    lines.append("")
    lines.append(f"Estimated Cost: ${cloud_stats.get('estimated_cost_usd', 0):.4f}")
    lines.append(f"Service Tier: {cloud_stats.get('service_tier', 'unknown')}")

    return "\n".join(lines)


def format_payload(data: dict) -> str:
    """Format payload summary for terminal display."""
    payload = data.get("payload_summary", {})
    files_sent = payload.get("files_sent", [])
    user_prompts = payload.get("user_prompts", [])

    lines = []

    # Files section
    lines.append(f"Files sent to cloud ({len(files_sent)}):")
    if files_sent:
        for f in files_sent:
            path = f.get("path", "unknown")
            size = f.get("size_chars", 0)
            timestamp = f.get("timestamp", "")
            time_str = ""
            if timestamp:
                try:
                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    time_str = f" @ {dt.strftime('%H:%M:%S')}"
                except:
                    pass
            lines.append(f"  - {path} ({size:,} chars){time_str}")
    else:
        lines.append("  (none)")

    lines.append("")

    # User prompts section
    lines.append(f"User prompts ({len(user_prompts)}):")
    if user_prompts:
        for i, p in enumerate(user_prompts, 1):
            text = p.get("text", "")[:80].replace("\n", " ")
            full_len = p.get("full_length", 0)
            if len(p.get("text", "")) > 80:
                text += "..."
            lines.append(f"  {i}. \"{text}\" ({full_len:,} chars)")
    else:
        lines.append("  (none)")

    lines.append("")

    # Summary
    total_chars = payload.get("total_content_chars", 0)
    lines.append(f"Total content: {total_chars:,} chars ({total_chars / 1024:.1f} KB)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session overview (topology)
#
# One entry point that shows everything a session left on disk:
#   <project>/<sid>.jsonl                      main transcript
#   <project>/<sid>/subagents/agent-*.jsonl    direct sub-agents (Agent tool)
#   <project>/<sid>/workflows/wf_*.json        dynamic workflow run snapshots
#   <project>/<sid>/subagents/workflows/<run>/ workflow agent transcripts + journal
#   <project>/<sid>/tool-results/              large tool outputs offloaded from context
#
# Token usage is NOT rolled up into the main transcript: the main file's Task
# tool result records only agentId/status. Each sub-agent's usage lives in its
# own transcript, so totals here aggregate across all of them.
# ---------------------------------------------------------------------------

def build_overview(main_file: Path) -> dict:
    """Build the session topology overview (see block comment above)."""
    project_dir = main_file.parent
    session_dir = project_dir / main_file.stem
    subagents_dir = session_dir / "subagents"
    workflows_dir = session_dir / "workflows"
    tool_results_dir = session_dir / "tool-results"

    main_entries = parse_jsonl(main_file)
    main_tokens = aggregate_tokens(main_entries)
    api_requests = sum(1 for e in main_entries if extract_usage(e))
    main_model = max(main_tokens["per_model"],
                     key=lambda m: sum(main_tokens["per_model"][m][k] for k in _TOKEN_FIELDS),
                     default="unknown")

    # Map agentId -> Task description (recorded in the main transcript's tool results)
    agent_desc = {}
    for e in main_entries:
        tur = e.get("toolUseResult")
        if isinstance(tur, dict) and tur.get("agentId"):
            agent_desc[tur["agentId"]] = tur.get("description") or ""

    aggs = [main_tokens]

    # Direct sub-agents (Agent tool). Workflow agents live one level deeper
    # and are attributed to their workflow run below, not double-counted here.
    subagents = []
    warmup_skipped = 0
    direct_files = sorted(subagents_dir.glob("agent-*.jsonl"),
                          key=lambda f: f.stat().st_mtime) if subagents_dir.is_dir() else []
    for f in direct_files:
        entries = parse_jsonl(f)
        if not entries:
            continue
        if is_warmup_agent(entries):
            warmup_skipped += 1
            continue
        agg = aggregate_tokens(entries)
        aggs.append(agg)
        agent_id = f.stem.replace("agent-", "", 1)
        agent_type = None
        meta_file = f.with_name(f"{f.stem}.meta.json")
        if meta_file.is_file():
            try:
                agent_type = json.loads(meta_file.read_text()).get("agentType")
            except (json.JSONDecodeError, OSError):
                pass
        subagents.append({
            "id": agent_id,
            "agent_type": agent_type or "subagent",
            "description": agent_desc.get(agent_id, ""),
            "path": str(f),
            "total_tokens": agg["total_tokens"],
            "cost": agg["cost"],
        })

    # Dynamic workflow runs (wf_*.json snapshots + their agent transcripts)
    workflows = []
    wf_files = sorted(workflows_dir.glob("wf_*.json"),
                      key=lambda f: f.stat().st_mtime) if workflows_dir.is_dir() else []
    for wf_json in wf_files:
        try:
            wf = json.loads(wf_json.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        run_id = wf.get("runId") or wf_json.stem
        wf_agents_dir = subagents_dir / "workflows" / run_id
        transcripts = sorted(wf_agents_dir.glob("agent-*.jsonl")) if wf_agents_dir.is_dir() else []
        wf_aggs = []
        for t in transcripts:
            entries = parse_jsonl(t)
            if entries and not is_warmup_agent(entries):
                wf_aggs.append(aggregate_tokens(entries))
        wf_agg = combine_token_aggregates(wf_aggs) if wf_aggs else None
        if wf_agg:
            aggs.append(wf_agg)
        workflows.append({
            "run_id": run_id,
            "name": wf.get("workflowName", ""),
            "status": wf.get("status", ""),
            "agent_count": wf.get("agentCount", 0),
            "reported_tokens": wf.get("totalTokens", 0),
            "duration_ms": wf.get("durationMs"),
            "snapshot": str(wf_json),
            "script": wf.get("scriptPath") or "",
            "agents_dir": str(wf_agents_dir) if transcripts else "",
            "transcript_count": len(transcripts),
            "transcript_tokens": wf_agg["total_tokens"] if wf_agg else 0,
            "cost": wf_agg["cost"] if wf_agg else 0.0,
        })

    tool_result_files = (sorted(f.name for f in tool_results_dir.iterdir() if f.is_file())
                         if tool_results_dir.is_dir() else [])
    combined = combine_token_aggregates(aggs)

    return {
        "session_id": main_file.stem,
        "transcript": str(main_file),
        "transcript_bytes": main_file.stat().st_size,
        "session_dir": str(session_dir) if session_dir.is_dir() else None,
        "main": {
            "model": main_model,
            "api_requests": api_requests,
            "total_tokens": main_tokens["total_tokens"],
            "cost": main_tokens["cost"],
        },
        "subagents": subagents,
        "warmup_agents_skipped": warmup_skipped,
        "workflows": workflows,
        "tool_results_dir": str(tool_results_dir) if tool_result_files else None,
        "tool_result_files": tool_result_files,
        "session_total": {
            "total_tokens": combined["total_tokens"],
            "cost": combined["cost"],
            "per_model": combined["per_model"],
        },
    }


def _fmt_duration_ms(ms) -> str:
    if ms is None:
        return "-"
    sec = ms / 1000
    return f"{sec:.0f}s" if sec < 60 else f"{sec / 60:.1f}m"


def format_overview(ov: dict) -> str:
    """Format a build_overview() dict as readable text."""
    lines = []
    lines.append("SESSION OVERVIEW")
    lines.append("=" * 60)
    lines.append(f"Session:     {ov['session_id']}")
    lines.append(f"Transcript:  {ov['transcript']}  ({ov['transcript_bytes'] / 1024:.0f} KB)")
    lines.append(f"Session dir: {ov['session_dir'] or '(none yet — created on first sub-agent, workflow, or offloaded tool result)'}")
    m = ov["main"]
    lines.append(f"Main agent:  {m['model']} · {m['api_requests']} API requests · "
                 f"{m['total_tokens']:,} tokens · est. ${m['cost']:.4f}")

    if ov["subagents"]:
        lines.append("")
        lines.append(f"DIRECT SUB-AGENTS ({len(ov['subagents'])})")
        lines.append("=" * 60)
        for a in ov["subagents"]:
            desc = f'  "{a["description"]}"' if a["description"] else ""
            lines.append(f"  {a['id'][:12]}  {a['agent_type']} · "
                         f"{a['total_tokens']:,} tokens · est. ${a['cost']:.4f}{desc}")
            lines.append(f"      {a['path']}")

    if ov["workflows"]:
        lines.append("")
        lines.append(f"DYNAMIC WORKFLOWS ({len(ov['workflows'])})")
        lines.append("=" * 60)
        for w in ov["workflows"]:
            lines.append(f"  {w['run_id']}  {w['name']}  [{w['status']}]  "
                         f"{w['agent_count']} agents · {w['reported_tokens']:,} tokens (reported) · "
                         f"{_fmt_duration_ms(w['duration_ms'])}")
            lines.append(f"      snapshot:  {w['snapshot']}")
            if w["script"]:
                lines.append(f"      script:    {w['script']}")
            if w["agents_dir"]:
                lines.append(f"      agents:    {w['agents_dir']}  "
                             f"({w['transcript_count']} transcripts · "
                             f"{w['transcript_tokens']:,} tokens · est. ${w['cost']:.4f})")
            lines.append(f"      describe:  claude-workflow-analyzer '{w['snapshot']}'")

    if ov["tool_result_files"]:
        lines.append("")
        lines.append(f"OFFLOADED TOOL RESULTS ({len(ov['tool_result_files'])})")
        lines.append("=" * 60)
        lines.append(f"  {ov['tool_results_dir']}")

    t = ov["session_total"]
    lines.append("")
    lines.append("SESSION TOTAL (main + sub-agents + workflow agents)")
    lines.append("=" * 60)
    lines.append(f"  {t['total_tokens']:,} tokens · est. ${t['cost']:.4f}")
    for model, pm in sorted(t["per_model"].items(), key=lambda kv: -kv[1]["cost"]):
        toks = sum(pm[k] for k in _TOKEN_FIELDS)
        lines.append(f"    {model}: {toks:,} tokens · est. ${pm['cost']:.4f}")
    lines.append("  (cost is an estimate — /cost and the Usage API are authoritative)")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Claude Code Session Analyzer")
    parser.add_argument("session_id", nargs="?",
                        help="Session ID, or path to a transcript .jsonl / per-session directory")
    parser.add_argument("--latest", action="store_true", help="Analyze most recent session")
    parser.add_argument("--list", action="store_true", help="List all sessions with summaries")
    parser.add_argument("--project", help="Project path to search in")
    parser.add_argument("--output", choices=["json", "html"], default="html",
                        help="Output format (default: html)")
    parser.add_argument("--digest", action="store_true",
                        help="Generate markdown digest for follow-up in new session")
    parser.add_argument("--cloud-stats", action="store_true",
                        help="Show cloud transmission summary (tokens, cost, API requests)")
    parser.add_argument("--payload", action="store_true",
                        help="Show what content was sent to the cloud (files, prompts)")
    parser.add_argument("--open", action="store_true", help="Open HTML in browser")
    parser.add_argument("--out-file", help="Write output to file instead of stdout")
    parser.add_argument("--path", action="store_true",
                        help="Print only the resolved transcript file path and exit")
    parser.add_argument("--overview", action="store_true",
                        help="Show session topology: transcript, sub-agents, "
                             "workflows, offloaded tool results, token totals")

    args = parser.parse_args()

    # Handle --path flag: resolve and print the transcript path only (fast, no parsing)
    if args.path:
        try:
            main_file, _ = find_session_files(
                session_id=args.session_id,
                project_path=args.project,
                latest=args.latest or (not args.session_id)
            )
            print(main_file)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # Handle --overview flag: session topology (main + sub-agents + workflows)
    if args.overview:
        try:
            main_file, _ = find_session_files(
                session_id=args.session_id,
                project_path=args.project,
                latest=args.latest or (not args.session_id)
            )
            ov = build_overview(main_file)
            print(json.dumps(ov, indent=2) if args.output == "json" else format_overview(ov))
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # Handle --list flag
    if args.list:
        try:
            sessions = list_sessions(project_path=args.project)
            if args.output == "json":
                print(json.dumps(sessions, indent=2))
            else:
                print(format_session_list(sessions))
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    try:
        main_file, agent_files = find_session_files(
            session_id=args.session_id,
            project_path=args.project,
            latest=args.latest or (not args.session_id)
        )

        print(f"Analyzing session: {main_file.name}", file=sys.stderr)
        print(f"Found {len(agent_files)} agent file(s)", file=sys.stderr)

        data = build_session_data(main_file, agent_files)

        # Get all sessions for the selector
        all_sessions = []
        try:
            all_sessions = list_sessions(project_path=args.project)
        except:
            pass  # Session list is optional

        if args.cloud_stats:
            output = format_cloud_stats(data)
        elif args.payload:
            output = format_payload(data)
        elif args.digest:
            output = generate_digest(data)
        elif args.output == "json":
            output = json.dumps(data, indent=2)
        else:
            output = generate_html(data, all_sessions=all_sessions)

        if args.out_file:
            with open(args.out_file, "w") as f:
                f.write(output)
            print(f"Written to {args.out_file}", file=sys.stderr)
            if args.open and args.output == "html":
                subprocess.run(["open", args.out_file])
        elif args.open and args.output == "html":
            # Write to temp file and open
            with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
                f.write(output)
                temp_path = f.name
            print(f"Opening {temp_path}", file=sys.stderr)
            subprocess.run(["open", temp_path])
        else:
            print(output)

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
