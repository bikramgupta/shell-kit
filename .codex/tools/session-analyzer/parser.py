#!/usr/bin/env python3
import argparse
import collections
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid

DEFAULT_CODEX_HOME = os.path.expanduser("~/.codex")
DEFAULT_SESSIONS_DIR = os.path.join(DEFAULT_CODEX_HOME, "sessions")

# Estimated per-1M-token USD prices — UPDATE as OpenAI pricing changes; these are ESTIMATES.
# Sourced from public OpenAI/Codex pricing pages (confirmed 2026-07 for gpt-5.2-codex:
# $1.75 input / $0.175 cached input / $14.00 output per 1M tokens). Cached input is
# billed cheaper than fresh input; reasoning_output_tokens are billed at the output rate.
# NOTE: gpt-5-codex cached_input is a best-guess estimate (~10% of input) and should be
# verified against the current OpenAI rate card before relying on these numbers.
PRICING_USD_PER_MTOK = {
    "gpt-5.2-codex": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
    "gpt-5.3-codex": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
    "gpt-5-codex": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    # Fallback for unknown/unlisted models — mirrors gpt-5.2-codex (the config.toml default).
    "default": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
}

SKIP_SUBSTRINGS = [
    "<environment_context>",
    "agents.md",
    "<instructions>",
    "skills are discovered",
    "trigger rules",
    "## skills",
]

CATEGORY_USER = "user"
CATEGORY_ASSISTANT = "assistant"
CATEGORY_TOOL = "tool"
CATEGORY_THINKING = "thinking"
CATEGORY_LIFECYCLE = "lifecycle"
CATEGORY_OTHER = "other"

EVENT_USER = "user"
EVENT_THINKING = "thinking"
EVENT_TEXT = "text"
EVENT_TOOL_USE = "tool_use"
EVENT_POST_TOOL = "post_tool_use"
EVENT_LIFECYCLE = "lifecycle"


def parse_ts(value):
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return dt.datetime.fromisoformat(value)
    except Exception:
        return None


def format_ts(ts):
    if not ts:
        return ""
    return ts.isoformat()


def extract_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") in ("input_text", "output_text"):
            parts.append(item.get("text", ""))
    return "".join(parts)


def is_instruction_text(text):
    if not text:
        return True
    lower = text.lower()
    return any(s in lower for s in SKIP_SUBSTRINGS)


def summarize_text(text, max_len=70):
    cleaned = re.sub(r"\s+", " ", text.strip())
    if not cleaned:
        return "(no summary)"
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3] + "..."


def truncate_text(text, max_chars):
    if not text:
        return ""
    if max_chars and len(text) > max_chars:
        return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"
    return text


def format_duration(start_ts, end_ts):
    if not start_ts or not end_ts:
        return "?"
    delta = end_ts - start_ts
    minutes = delta.total_seconds() / 60.0
    return f"{minutes:.1f}m"


def iter_session_files(sessions_dir):
    for root, _, files in os.walk(sessions_dir):
        for name in files:
            if name.startswith("rollout-") and name.endswith(".jsonl"):
                yield os.path.join(root, name)


def indent_block(text, prefix="  "):
    if not text:
        return ""
    return "\n".join(prefix + line for line in text.splitlines())


def stringify_details(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)
    except Exception:
        return str(value)


def extract_reasoning_text(payload):
    parts = []
    summary = payload.get("summary")
    if isinstance(summary, list):
        for item in summary:
            if isinstance(item, dict) and "summary_text" in item:
                parts.append(item.get("summary_text") or "")
            elif isinstance(item, str):
                parts.append(item)
    if payload.get("content"):
        parts.append(payload.get("content"))
    if payload.get("encrypted_content"):
        parts.append("(encrypted_content present)")
    return "\n".join(p for p in parts if p)


def maybe_parse_json(value):
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except Exception:
                return value
    return value


def format_kv_line(label, data, keys):
    parts = []
    for key in keys:
        if key in data and data[key] is not None:
            parts.append(f"{key}={data[key]}")
    if not parts:
        return ""
    return f"{label}: " + ", ".join(parts)


def format_token_count(payload):
    info = payload.get("info") if isinstance(payload, dict) else None
    info = info or {}
    total = info.get("total_token_usage") or {}
    last = info.get("last_token_usage") or {}
    lines = []
    total_line = format_kv_line(
        "total",
        total,
        [
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        ],
    )
    last_line = format_kv_line(
        "last",
        last,
        [
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        ],
    )
    if total_line:
        lines.append(total_line)
    if last_line:
        lines.append(last_line)
    if info.get("model_context_window") is not None:
        lines.append(f"context_window={info.get('model_context_window')}")
    return "\n".join(lines)


def summarize_token_count(payload):
    info = payload.get("info") if isinstance(payload, dict) else None
    info = info or {}
    total = info.get("total_token_usage") or {}
    last = info.get("last_token_usage") or {}
    total_tokens = total.get("total_tokens")
    last_tokens = last.get("total_tokens")
    parts = []
    if total_tokens is not None:
        parts.append(f"total={total_tokens}")
    if last_tokens is not None:
        parts.append(f"last={last_tokens}")
    suffix = " ".join(parts)
    return f"token_count {suffix}".strip() if suffix else "token_count"


def estimate_cost(model, total_usage):
    """Estimate USD cost from a session's final total_token_usage.

    Looks the model up in PRICING_USD_PER_MTOK (falling back to "default"), then:
        cost = input_tokens * input_rate
             + cached_input_tokens * cached_rate
             + (output_tokens + reasoning_output_tokens) * output_rate
    all divided by 1e6. cached_input_tokens and input_tokens are treated as distinct
    billing buckets here. Returns None when usage is empty/unavailable. This is an
    ESTIMATE only and is deliberately defensive — it never raises.
    """
    try:
        usage = total_usage if isinstance(total_usage, dict) else {}
        if not usage:
            return None

        def _num(key):
            value = usage.get(key)
            return value if isinstance(value, (int, float)) else 0

        input_tokens = _num("input_tokens")
        cached_input_tokens = _num("cached_input_tokens")
        output_tokens = _num("output_tokens")
        reasoning_output_tokens = _num("reasoning_output_tokens")

        if not any(
            [input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens]
        ):
            return None

        rates = (
            PRICING_USD_PER_MTOK.get(model)
            or PRICING_USD_PER_MTOK.get("default")
            or {}
        )
        input_rate = rates.get("input", 0)
        cached_rate = rates.get("cached_input", 0)
        output_rate = rates.get("output", 0)

        cost = (
            input_tokens * input_rate
            + cached_input_tokens * cached_rate
            + (output_tokens + reasoning_output_tokens) * output_rate
        ) / 1_000_000.0
        return cost
    except Exception:
        return None


def format_cost_usd(cost):
    """Format an estimated cost as a short USD string, or '-' when unknown."""
    if cost is None:
        return "-"
    return f"${cost:.4f}"


def format_session_meta(payload):
    if not isinstance(payload, dict):
        return ""
    keys = ["id", "cwd", "originator", "cli_version", "model_provider", "source"]
    lines = []
    for key in keys:
        if key in payload:
            lines.append(f"{key}: {payload[key]}")
    return "\n".join(lines)


def summarize_session_meta(payload):
    if not isinstance(payload, dict):
        return "session_meta"
    session_id = payload.get("id")
    cwd = payload.get("cwd")
    pieces = ["session_meta"]
    if session_id:
        pieces.append(f"id={session_id[:8]}")
    if cwd:
        pieces.append(f"cwd={cwd}")
    return " ".join(pieces)


def format_turn_context(payload):
    if not isinstance(payload, dict):
        return ""
    lines = []
    if payload.get("cwd"):
        lines.append(f"cwd: {payload.get('cwd')}")
    if payload.get("model"):
        lines.append(f"model: {payload.get('model')}")
    if payload.get("effort"):
        lines.append(f"effort: {payload.get('effort')}")
    if payload.get("approval_policy"):
        lines.append(f"approval_policy: {payload.get('approval_policy')}")
    sandbox = payload.get("sandbox_policy") or {}
    if isinstance(sandbox, dict) and sandbox.get("type"):
        lines.append(f"sandbox: {sandbox.get('type')}")
    return "\n".join(lines)


def summarize_turn_context(payload):
    if not isinstance(payload, dict):
        return "turn_context"
    pieces = ["turn_context"]
    if payload.get("cwd"):
        pieces.append(f"cwd={payload.get('cwd')}")
    if payload.get("model"):
        pieces.append(f"model={payload.get('model')}")
    return " ".join(pieces)


def format_tool_args(args):
    parsed = maybe_parse_json(args)
    if isinstance(parsed, dict):
        preferred = ["command", "url", "file_path", "path", "uri", "workdir", "timeout_ms", "justification"]
        keys = sorted(parsed.keys(), key=lambda k: (k not in preferred, preferred.index(k) if k in preferred else 999, k))
        return "\n".join(f"{key}: {parsed[key]}" for key in keys)
    if isinstance(parsed, list):
        return "\n".join(str(item) for item in parsed)
    return stringify_details(parsed)


def summarize_tool_args(args):
    parsed = maybe_parse_json(args)
    if isinstance(parsed, dict):
        for key in ["command", "url", "file_path", "path", "uri"]:
            if key in parsed:
                return f"{key}={parsed[key]}"
    return summarize_text(stringify_details(parsed), max_len=120)


def summarize_tool_output(output):
    if not output:
        return ""
    # Real session data sometimes carries `output` as a list/dict, not a string.
    if not isinstance(output, str):
        output = stringify_details(output)
    exit_match = re.search(r"Exit code:\s*(\d+)", output)
    exit_code = exit_match.group(1) if exit_match else None
    first_line = ""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if "Output:" in lines:
        idx = lines.index("Output:")
        if idx + 1 < len(lines):
            first_line = lines[idx + 1]
    elif lines:
        first_line = lines[0]
    pieces = []
    if exit_code is not None:
        pieces.append(f"exit={exit_code}")
    if first_line:
        pieces.append(first_line[:120])
    return " ".join(pieces) if pieces else summarize_text(output, max_len=120)


def tool_output_error(output):
    if not output:
        return False
    if not isinstance(output, str):
        output = stringify_details(output)
    match = re.search(r"Exit code:\s*(\d+)", output)
    if match:
        return match.group(1) != "0"
    return False


def parse_session(path, max_chars=0, include_raw=False):
    meta = {}
    tool_counts = collections.Counter()
    user_messages = []
    assistant_messages = []
    first_ts = None
    last_ts = None
    model = None
    cli_version = None
    token_usage = None
    events = []
    index = 0

    def add_event(
        ts,
        category,
        source,
        kind,
        role="",
        name="",
        summary="",
        details="",
        raw="",
        event_type="",
        error=False,
    ):
        nonlocal index
        event = {
            "index": index,
            "ts": format_ts(ts),
            "epoch": ts.timestamp() if ts else 0,
            "category": category,
            "source": source,
            "kind": kind,
            "role": role,
            "name": name,
            "summary": summary,
            "details": details,
            "raw": raw,
            "event_type": event_type,
            "error": error,
        }
        events.append(event)
        index += 1

    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue

            ts = parse_ts(obj.get("timestamp"))
            if ts:
                if not first_ts or ts < first_ts:
                    first_ts = ts
                if not last_ts or ts > last_ts:
                    last_ts = ts

            raw_json = ""
            if include_raw:
                try:
                    raw_json = json.dumps(obj, indent=2, ensure_ascii=False)
                except Exception:
                    raw_json = ""

            obj_type = obj.get("type")
            if obj_type == "session_meta":
                meta = obj.get("payload", {}) or {}
                cli_version = meta.get("cli_version") or cli_version
                details = format_session_meta(meta)
                details = truncate_text(details, max_chars)
                add_event(
                    ts,
                    CATEGORY_LIFECYCLE,
                    "session_meta",
                    "session_meta",
                    summary=summarize_session_meta(meta),
                    details=details,
                    raw=raw_json,
                    event_type=EVENT_LIFECYCLE,
                )
                continue

            if obj_type == "turn_context":
                payload = obj.get("payload", {}) or {}
                model = payload.get("model") or model
                details = format_turn_context(payload)
                details = truncate_text(details, max_chars)
                add_event(
                    ts,
                    CATEGORY_LIFECYCLE,
                    "turn_context",
                    "turn_context",
                    summary=summarize_turn_context(payload),
                    details=details,
                    raw=raw_json,
                    event_type=EVENT_LIFECYCLE,
                )
                continue

            if obj_type == "response_item":
                payload = obj.get("payload", {}) or {}
                payload_type = payload.get("type")
                if payload_type == "message":
                    role = payload.get("role") or ""
                    text = extract_text(payload.get("content"))
                    details = truncate_text(text, max_chars)
                    if role == "user":
                        category = CATEGORY_USER
                        user_messages.append(text)
                        event_type = EVENT_USER
                    elif role == "assistant":
                        category = CATEGORY_ASSISTANT
                        assistant_messages.append(text)
                        event_type = EVENT_TEXT
                    else:
                        category = CATEGORY_OTHER
                        event_type = EVENT_TEXT
                    summary = summarize_text(text, max_len=120)
                    add_event(
                        ts,
                        category,
                        "response_item",
                        "message",
                        role=role,
                        summary=summary,
                        details=details,
                        raw=raw_json,
                        event_type=event_type,
                    )
                elif payload_type == "function_call":
                    name = payload.get("name", "unknown")
                    args = payload.get("arguments")
                    args_summary = summarize_tool_args(args)
                    details = format_tool_args(args)
                    details = truncate_text(details, max_chars)
                    tool_counts[name] += 1
                    summary = f"{name}: {args_summary}" if args_summary else name
                    add_event(
                        ts,
                        CATEGORY_TOOL,
                        "response_item",
                        "function_call",
                        name=name,
                        summary=summary,
                        details=details,
                        raw=raw_json,
                        event_type=EVENT_TOOL_USE,
                    )
                elif payload_type == "function_call_output":
                    output = payload.get("output", "")
                    details = truncate_text(output, max_chars)
                    summary = summarize_tool_output(output)
                    call_id = payload.get("call_id") or ""
                    add_event(
                        ts,
                        CATEGORY_TOOL,
                        "response_item",
                        "function_call_output",
                        name=call_id,
                        summary=summary,
                        details=details,
                        raw=raw_json,
                        event_type=EVENT_POST_TOOL,
                        error=tool_output_error(output),
                    )
                elif payload_type == "reasoning":
                    reasoning_text = extract_reasoning_text(payload)
                    details = truncate_text(reasoning_text, max_chars)
                    summary = summarize_text(reasoning_text or "reasoning", max_len=120)
                    add_event(
                        ts,
                        CATEGORY_THINKING,
                        "response_item",
                        "reasoning",
                        summary=summary,
                        details=details,
                        raw=raw_json,
                        event_type=EVENT_THINKING,
                    )
                elif payload_type == "ghost_snapshot":
                    details = summarize_text(stringify_details(payload), max_len=200)
                    add_event(
                        ts,
                        CATEGORY_LIFECYCLE,
                        "response_item",
                        "ghost_snapshot",
                        summary="ghost_snapshot",
                        details=details,
                        raw=raw_json,
                        event_type=EVENT_LIFECYCLE,
                    )
                continue

            if obj_type == "event_msg":
                payload = obj.get("payload", {}) or {}
                event_type_payload = payload.get("type")
                if event_type_payload == "user_message":
                    text = payload.get("message", "")
                    details = truncate_text(text, max_chars)
                    summary = summarize_text(text, max_len=120)
                    user_messages.append(text)
                    add_event(
                        ts,
                        CATEGORY_USER,
                        "event_msg",
                        "user_message",
                        role="user",
                        summary=summary,
                        details=details,
                        raw=raw_json,
                        event_type=EVENT_USER,
                    )
                elif event_type_payload == "agent_message":
                    text = payload.get("message", "")
                    details = truncate_text(text, max_chars)
                    summary = summarize_text(text, max_len=120)
                    assistant_messages.append(text)
                    add_event(
                        ts,
                        CATEGORY_ASSISTANT,
                        "event_msg",
                        "agent_message",
                        role="assistant",
                        summary=summary,
                        details=details,
                        raw=raw_json,
                        event_type=EVENT_TEXT,
                    )
                elif event_type_payload == "agent_reasoning":
                    text = payload.get("text", "")
                    details = truncate_text(text, max_chars)
                    summary = summarize_text(text or "agent_reasoning", max_len=120)
                    add_event(
                        ts,
                        CATEGORY_THINKING,
                        "event_msg",
                        "agent_reasoning",
                        summary=summary,
                        details=details,
                        raw=raw_json,
                        event_type=EVENT_THINKING,
                    )
                elif event_type_payload == "token_count":
                    # token_count events report cumulative totals; keep the latest as
                    # the session's final usage for cost estimation.
                    info = payload.get("info") or {}
                    total_usage = info.get("total_token_usage")
                    if isinstance(total_usage, dict) and total_usage:
                        token_usage = total_usage
                    details = format_token_count(payload)
                    details = truncate_text(details, max_chars)
                    summary = summarize_token_count(payload)
                    add_event(
                        ts,
                        CATEGORY_LIFECYCLE,
                        "event_msg",
                        "token_count",
                        summary=summary,
                        details=details,
                        raw=raw_json,
                        event_type=EVENT_LIFECYCLE,
                    )
                else:
                    details = summarize_text(stringify_details(payload), max_len=200)
                    add_event(
                        ts,
                        CATEGORY_OTHER,
                        "event_msg",
                        event_type_payload or "event_msg",
                        summary=event_type_payload or "event_msg",
                        details=details,
                        raw=raw_json,
                        event_type=EVENT_LIFECYCLE,
                    )
                continue

    session_id = meta.get("id") or os.path.basename(path)
    cwd = meta.get("cwd")
    counts_by_category = collections.Counter(e["category"] for e in events)
    counts_by_kind = collections.Counter(e["kind"] for e in events)
    counts_by_source = collections.Counter(e["source"] for e in events)
    counts_by_event = collections.Counter(e["event_type"] for e in events)

    events_sorted = sorted(events, key=lambda e: (e["epoch"], e["index"]))

    return {
        "id": session_id,
        "path": path,
        "cwd": cwd,
        "start_ts": first_ts,
        "end_ts": last_ts,
        "duration": format_duration(first_ts, last_ts),
        "user_messages": user_messages,
        "assistant_messages": assistant_messages,
        "tool_counts": tool_counts,
        "model": model,
        "cli_version": cli_version,
        "token_usage": token_usage,
        "estimated_cost": estimate_cost(model, token_usage),
        "summary": summarize_text(first_non_instruction(user_messages)),
        "events": events_sorted,
        "counts_by_category": counts_by_category,
        "counts_by_kind": counts_by_kind,
        "counts_by_source": counts_by_source,
        "counts_by_event": counts_by_event,
    }


def first_non_instruction(messages):
    for msg in messages:
        if msg and not is_instruction_text(msg):
            return msg
    return ""


def select_session(sessions, needle):
    if not needle:
        return None, []
    matches = []
    for session in sessions:
        sid = session["id"]
        if sid.startswith(needle):
            matches.append(session)
            continue
        if needle in session["path"]:
            matches.append(session)
            continue
    return (matches[0] if len(matches) == 1 else None), matches


def print_list(sessions, limit=None):
    sessions = sorted(sessions, key=lambda s: s["start_ts"] or dt.datetime.min, reverse=True)
    if limit:
        sessions = sessions[:limit]
    print("ID        Duration   Est.Cost  Project                        Summary")
    print("-" * 100)
    for session in sessions:
        sid = (session["id"] or "")[:8]
        duration = session["duration"]
        project = session["cwd"] or "(unknown)"
        summary = session["summary"]
        cost = session.get("estimated_cost")
        if cost is None:
            cost = estimate_cost(session.get("model"), session.get("token_usage"))
        cost_str = format_cost_usd(cost)  # ESTIMATE — see /cost for authoritative billing
        print(f"{sid:<10}{duration:>8}  {cost_str:>9}  {project:<30}  {summary}")


def generate_digest(session):
    """Generate a concise markdown digest for feeding into a new session."""
    lines = []

    sid = session["id"]
    short_id = sid[:8] if sid else "(unknown)"
    events = session["events"]

    # Header
    lines.append(f"# Session Digest: {short_id}")
    lines.append("")
    lines.append(f"**Project:** {session['cwd'] or '(unknown)'}")
    lines.append(f"**Duration:** {session['duration']}")
    if session["model"]:
        lines.append(f"**Model:** {session['model']}")
    cost = session.get("estimated_cost")
    if cost is None:
        cost = estimate_cost(session.get("model"), session.get("token_usage"))
    if cost is not None:
        lines.append(
            f"**Estimated cost:** {format_cost_usd(cost)} "
            "(estimate — see /cost or provider billing for authoritative)"
        )
    lines.append("")

    # User prompts (filter out instruction messages)
    user_events = [e for e in events if e["event_type"] == EVENT_USER]
    user_prompts = []
    for e in user_events:
        text = e.get("details") or e.get("summary") or ""
        if text and not is_instruction_text(text):
            user_prompts.append(text)

    if user_prompts:
        lines.append("## User Prompts (chronological)")
        lines.append("")
        for i, text in enumerate(user_prompts[:10], 1):
            # Clean and truncate
            text = re.sub(r"\s+", " ", text.strip())[:300]
            if len(text) == 300:
                text += "..."
            lines.append(f'{i}. "{text}"')
        if len(user_prompts) > 10:
            lines.append(f"   ... and {len(user_prompts) - 10} more prompts")
        lines.append("")

    # Key thinking (extract most substantive ones)
    thinking_events = [e for e in events if e["event_type"] == EVENT_THINKING]
    if thinking_events:
        lines.append("## Key Thinking/Decisions")
        lines.append("")
        # Sort by length of details to get most substantive
        substantive = sorted(thinking_events, key=lambda x: len(x.get("details") or ""), reverse=True)[:5]
        for block in substantive:
            text = block.get("details") or block.get("summary") or ""
            text = re.sub(r"\s+", " ", text.strip())[:400]
            if len(text) == 400:
                text += "..."
            if text:
                lines.append(f"- {text}")
        lines.append("")

    # Commands run (from tool calls)
    tool_events = [e for e in events if e["event_type"] == EVENT_TOOL_USE]
    commands = []
    files_modified = []

    for e in tool_events:
        details = e.get("details") or ""
        # Extract command from details
        cmd_match = re.search(r"command:\s*(.+?)(?:\n|$)", details, re.IGNORECASE)
        if cmd_match:
            cmd = cmd_match.group(1).strip()[:100]
            if cmd and cmd not in commands:
                commands.append(cmd)
        # Extract file paths
        for key in ["file_path", "path"]:
            path_match = re.search(rf"{key}:\s*(.+?)(?:\n|$)", details, re.IGNORECASE)
            if path_match:
                fpath = path_match.group(1).strip()
                if fpath and fpath not in files_modified:
                    files_modified.append(fpath)

    if commands:
        lines.append("## Commands Run")
        lines.append("")
        for cmd in commands[:15]:
            lines.append(f"- `{cmd}`")
        lines.append("")

    if files_modified:
        lines.append("## Files Modified")
        lines.append("")
        for fpath in files_modified[:15]:
            # Shorten if needed
            if len(fpath) > 60:
                fpath = "..." + fpath[-57:]
            lines.append(f"- {fpath}")
        lines.append("")

    # Errors encountered
    error_events = [e for e in events if e["event_type"] == EVENT_POST_TOOL and e.get("error")]
    if error_events:
        lines.append("## Errors Encountered")
        lines.append("")
        for err in error_events[:5]:
            name = err.get("name") or "tool"
            summary = err.get("summary") or ""
            summary = re.sub(r"\s+", " ", summary.strip())[:200]
            lines.append(f"- **{name}**: {summary}")
        lines.append("")

    # Where it left off
    lines.append("## Where It Left Off")
    lines.append("")

    # Last user prompt
    if user_prompts:
        last_prompt = user_prompts[-1][:500]
        lines.append("**Last user request:**")
        lines.append(f"> {last_prompt}")
        lines.append("")

    # Last assistant response
    text_events = [e for e in events if e["event_type"] == EVENT_TEXT]
    if text_events:
        last_text = text_events[-1].get("details") or text_events[-1].get("summary") or ""
        last_text = last_text[:500]
        last_text = last_text.replace("\n", "\n> ")
        lines.append("**Last assistant response:**")
        lines.append(f"> {last_text}")
        lines.append("")

    return "\n".join(lines)


def build_html(session, sessions):
    events = session["events"]

    events_json = json.dumps(events, ensure_ascii=False)
    events_json = events_json.replace("</", "<\\/")

    sessions_payload = []
    for item in sessions:
        sessions_payload.append(
            {
                "id": item["id"],
                "summary": item["summary"],
                "duration": item["duration"],
                "project": item["cwd"],
            }
        )
    sessions_json = json.dumps(sessions_payload, ensure_ascii=False)
    sessions_json = sessions_json.replace("</", "<\\/")

    sources = sorted(session["counts_by_source"].keys())
    source_options = "".join(f"<option value=\"{s}\">{s}</option>" for s in sources)

    def html_escape(value):
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Codex Session: __SESSION_SHORT__</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #1a1a2e;
      color: #eee;
      padding: 20px;
      line-height: 1.5;
    }
    .header {
      background: #16213e;
      padding: 20px;
      border-radius: 8px;
      margin-bottom: 20px;
    }
    .header-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 10px;
      gap: 12px;
      flex-wrap: wrap;
    }
    .header h1 { font-size: 1.5em; margin-bottom: 10px; }
    .session-selector {
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 5px;
    }
    .session-selector select {
      padding: 8px 12px;
      background: #0f3460;
      border: 1px solid #333;
      color: #eee;
      border-radius: 4px;
      font-size: 14px;
      max-width: 380px;
    }
    .session-selector .hint {
      font-size: 0.75em;
      color: #666;
    }
    .session-cmd {
      display: none;
      margin-top: 5px;
      padding: 8px 12px;
      background: #0a1628;
      border-radius: 4px;
      font-family: monospace;
      font-size: 0.85em;
      color: #4da8da;
    }
    .session-cmd.visible { display: block; }
    .header .meta { color: #888; font-size: 0.9em; }
    .header .meta div { margin-bottom: 4px; }
    .stats {
      display: flex;
      gap: 15px;
      margin-top: 15px;
      flex-wrap: wrap;
    }
    .stat {
      background: #0f3460;
      padding: 10px 15px;
      border-radius: 6px;
    }
    .stat-value { font-size: 1.3em; font-weight: bold; color: #e94560; }
    .stat-label { font-size: 0.75em; color: #888; }
    .filter-bar {
      margin-bottom: 15px;
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    .filter-bar select, .filter-bar input, .filter-bar label {
      padding: 8px 12px;
      background: #16213e;
      border: 1px solid #333;
      color: #eee;
      border-radius: 4px;
      font-size: 14px;
    }
    .filter-bar label {
      display: flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
    }
    .filter-bar input[type="checkbox"] {
      width: 16px;
      height: 16px;
    }
    .tabs {
      display: flex;
      gap: 10px;
      margin-bottom: 20px;
      flex-wrap: wrap;
    }
    .tab {
      padding: 10px 20px;
      background: #16213e;
      border: none;
      color: #888;
      cursor: pointer;
      border-radius: 6px;
      font-size: 1em;
    }
    .tab.active { background: #e94560; color: white; }
    .view { display: none; }
    .view.active { display: block; }

    .timeline { padding: 10px; max-width: 1200px; }
    .trace-item {
      margin-bottom: 8px;
      border-radius: 6px;
      overflow: hidden;
    }
    .trace-header {
      display: flex;
      align-items: flex-start;
      padding: 10px 12px;
      cursor: pointer;
      gap: 10px;
    }
    .trace-header:hover { filter: brightness(1.1); }
    .trace-time {
      width: 85px;
      flex-shrink: 0;
      font-size: 0.75em;
      color: #666;
      font-family: monospace;
    }
    .trace-type {
      width: 95px;
      flex-shrink: 0;
      padding: 2px 6px;
      border-radius: 3px;
      font-size: 0.7em;
      text-align: center;
      font-weight: bold;
    }
    .trace-summary {
      flex: 1;
      font-size: 0.9em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .trace-detail {
      display: none;
      padding: 10px 12px 12px 12px;
      background: rgba(0,0,0,0.2);
      font-size: 0.85em;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 340px;
      overflow-y: auto;
      font-family: monospace;
      color: #aaa;
    }
    .trace-item.expanded .trace-detail { display: block; }

    .trace-thinking { background: #1e293b; }
    .trace-thinking .trace-type { background: #6366f1; color: white; }
    .trace-text { background: #1a2e1a; }
    .trace-text .trace-type { background: #22c55e; color: white; }
    .trace-user { background: #1e3a5f; }
    .trace-user .trace-type { background: #3b82f6; color: white; }
    .trace-tool_use { background: #16213e; }
    .trace-tool_use .trace-type { background: #e94560; color: white; }
    .trace-lifecycle { background: #2d1f3d; }
    .trace-lifecycle .trace-type { background: #9333ea; color: white; }
    .trace-post_tool_use { background: #1a2e1a; }
    .trace-post_tool_use .trace-type { background: #22c55e; color: white; }
    .trace-post_tool_use.error { background: #2e1a1a; }
    .trace-post_tool_use.error .trace-type { background: #ef4444; color: white; }

    .tree { padding: 10px; }
    .tree-agent {
      margin-bottom: 15px;
      background: #16213e;
      border-radius: 8px;
      overflow: hidden;
    }
    .tree-agent-header {
      padding: 12px 15px;
      cursor: pointer;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .tree-agent-header:hover { background: #1e2a4a; }
    .tree-agent-name { font-weight: bold; }
    .tree-agent-stats { font-size: 0.85em; color: #888; }
    .tree-agent-trace {
      padding: 0 15px 15px 15px;
      display: none;
      max-height: 500px;
      overflow-y: auto;
    }
    .tree-agent.expanded .tree-agent-trace { display: block; }
    .tree-item {
      padding: 6px 10px;
      margin-top: 4px;
      border-radius: 4px;
      font-size: 0.85em;
    }
    .tree-item-user { background: #1e3a5f; border-left: 3px solid #3b82f6; }
    .tree-item-thinking { background: #1e293b; border-left: 3px solid #6366f1; }
    .tree-item-text { background: #1a2e1a; border-left: 3px solid #22c55e; }
    .tree-item-tool_use { background: #0f3460; border-left: 3px solid #e94560; }
    .tree-item-lifecycle { background: #2d1f3d; border-left: 3px solid #9333ea; }
    .tree-item-post_tool_use { background: #1a2e1a; border-left: 3px solid #22c55e; }
    .tree-item-post_tool_use.error { border-left-color: #ef4444; background: #2e1a1a; }
    .tree-item-time { color: #666; font-family: monospace; font-size: 0.8em; }
    .tree-item-label { font-weight: bold; margin-left: 8px; }
    .tree-item-content { margin-top: 4px; color: #aaa; font-size: 0.9em; }
    .expand-icon { transition: transform 0.2s; display: inline-block; margin-right: 8px; }
    .tree-agent.expanded .expand-icon { transform: rotate(90deg); }

    .empty {
      padding: 16px;
      color: #888;
      font-style: italic;
    }
  </style>
</head>
<body>
  <div class="header">
    <div class="header-top">
      <div>
        <h1>Session Trace Viewer</h1>
        <div class="meta">
          <div>Project: __PROJECT__</div>
          <div>Duration: __DURATION__</div>
          <div>Model: __MODEL__</div>
          <div>CLI: __CLI__</div>
        </div>
      </div>
      <div class="session-selector">
        <select id="sessionSelect">
          <option value="__SESSION_ID__">__SESSION_SHORT__ - (current)</option>
        </select>
        <div class="hint">Select a session to view</div>
        <div id="sessionCmd" class="session-cmd"></div>
      </div>
    </div>
    <div class="stats">
      <div class="stat">
        <div class="stat-value" id="statToolCalls">-</div>
        <div class="stat-label">Tool Calls</div>
      </div>
      <div class="stat">
        <div class="stat-value" id="statUser">-</div>
        <div class="stat-label">User Prompts</div>
      </div>
      <div class="stat">
        <div class="stat-value" id="statThinking">-</div>
        <div class="stat-label">Thinking</div>
      </div>
      <div class="stat">
        <div class="stat-value" id="statText">-</div>
        <div class="stat-label">Responses</div>
      </div>
      <div class="stat">
        <div class="stat-value" id="statLifecycle">-</div>
        <div class="stat-label">Lifecycle</div>
      </div>
    </div>
  </div>

  <div class="filter-bar">
    <select id="eventFilter">
      <option value="">All Events</option>
      <option value="user">User Prompt</option>
      <option value="thinking">Thinking</option>
      <option value="tool_use">Tool Use</option>
      <option value="post_tool_use">Tool Output</option>
      <option value="text">Text Response</option>
      <option value="lifecycle">Lifecycle</option>
    </select>
    <select id="sourceFilter">
      <option value="">All Sources</option>
      __SOURCE_OPTIONS__
    </select>
    <input type="text" id="searchInput" placeholder="Search...">
    <label><input type="checkbox" id="showUser" checked> User</label>
    <label><input type="checkbox" id="showThinking" checked> Thinking</label>
    <label><input type="checkbox" id="showText" checked> Responses</label>
    <label><input type="checkbox" id="showTools" checked> Tools</label>
    <label><input type="checkbox" id="showLifecycle" checked> Lifecycle</label>
    <label><input type="checkbox" id="showRaw"> Raw JSON</label>
  </div>

  <div class="tabs">
    <button class="tab active" data-view="timeline">Timeline</button>
    <button class="tab" data-view="tree">Tree</button>
    <button class="tab" id="expandAll">Expand all</button>
    <button class="tab" id="collapseAll">Collapse all</button>
  </div>

  <div id="timeline" class="view active">
    <div class="timeline"></div>
  </div>

  <div id="tree" class="view">
    <div class="tree"></div>
  </div>

  <script>
    try {
      const events = __EVENTS_JSON__;
      const allSessions = __SESSIONS_JSON__;
      const currentSessionId = '__SESSION_ID__';

      const sessionSelect = document.getElementById('sessionSelect');
      const sessionCmd = document.getElementById('sessionCmd');

      function shortId(value) {
        return value ? value.substring(0, 8) : '';
      }

      if (allSessions && allSessions.length > 0) {
        sessionSelect.innerHTML = '';
        allSessions.forEach(s => {
          const opt = document.createElement('option');
          opt.value = s.id;
          const summary = s.summary ? s.summary.substring(0, 40) : '(no summary)';
          const duration = s.duration ? s.duration : '?';
          opt.textContent = shortId(s.id) + ' - ' + duration + ' - ' + summary;
          if (s.id === currentSessionId) {
            opt.selected = true;
            opt.textContent += ' (current)';
          }
          sessionSelect.appendChild(opt);
        });
      }

      sessionSelect.addEventListener('change', function() {
        const selectedId = this.value;
        if (selectedId !== currentSessionId) {
          sessionCmd.textContent = 'Run: codex-session-analyzer ' + shortId(selectedId) + ' --open';
          sessionCmd.classList.add('visible');
        } else {
          sessionCmd.classList.remove('visible');
        }
      });

      const eventFilter = document.getElementById('eventFilter');
      const sourceFilter = document.getElementById('sourceFilter');
      const searchInput = document.getElementById('searchInput');
      const showUser = document.getElementById('showUser');
      const showThinking = document.getElementById('showThinking');
      const showText = document.getElementById('showText');
      const showTools = document.getElementById('showTools');
      const showLifecycle = document.getElementById('showLifecycle');
      const showRaw = document.getElementById('showRaw');

      const timelineEl = document.querySelector('.timeline');
      const treeEl = document.querySelector('.tree');

      const statToolCalls = document.getElementById('statToolCalls');
      const statUser = document.getElementById('statUser');
      const statThinking = document.getElementById('statThinking');
      const statText = document.getElementById('statText');
      const statLifecycle = document.getElementById('statLifecycle');

      const tabs = document.querySelectorAll('.tab[data-view]');
      const views = document.querySelectorAll('.view');

      tabs.forEach(tab => {
        tab.addEventListener('click', () => {
          tabs.forEach(t => t.classList.remove('active'));
          tab.classList.add('active');
          views.forEach(view => view.classList.remove('active'));
          document.getElementById(tab.dataset.view).classList.add('active');
        });
      });

      document.getElementById('expandAll').addEventListener('click', () => {
        document.querySelectorAll('.trace-item').forEach(item => item.classList.add('expanded'));
        document.querySelectorAll('.tree-agent').forEach(item => item.classList.add('expanded'));
      });

      document.getElementById('collapseAll').addEventListener('click', () => {
        document.querySelectorAll('.trace-item').forEach(item => item.classList.remove('expanded'));
        document.querySelectorAll('.tree-agent').forEach(item => item.classList.remove('expanded'));
      });

      function formatTime(ts) {
        if (!ts) return '';
        const d = new Date(ts);
        return d.toLocaleTimeString();
      }

      function formatTypeLabel(eventType) {
        if (eventType === 'post_tool_use') return 'tool_output';
        return eventType || 'event';
      }

      function computeStats() {
        const counts = { user: 0, thinking: 0, text: 0, tool_use: 0, lifecycle: 0 };
        events.forEach(e => {
          if (e.event_type && counts.hasOwnProperty(e.event_type)) {
            counts[e.event_type] += 1;
          }
        });
        statToolCalls.textContent = counts.tool_use || 0;
        statUser.textContent = counts.user || 0;
        statThinking.textContent = counts.thinking || 0;
        statText.textContent = counts.text || 0;
        statLifecycle.textContent = counts.lifecycle || 0;
      }

      function shouldShowEvent(event) {
        if (eventFilter.value && event.event_type !== eventFilter.value) return false;
        if (sourceFilter.value && event.source !== sourceFilter.value) return false;

        const query = searchInput.value.trim().toLowerCase();
        if (query) {
          const haystack = [event.summary, event.details, event.raw, event.kind, event.role, event.name, event.source]
            .join(' ')
            .toLowerCase();
          if (!haystack.includes(query)) return false;
        }

        if (event.event_type === 'user' && !showUser.checked) return false;
        if (event.event_type === 'thinking' && !showThinking.checked) return false;
        if (event.event_type === 'text' && !showText.checked) return false;
        if ((event.event_type === 'tool_use' || event.event_type === 'post_tool_use') && !showTools.checked) return false;
        if (event.event_type === 'lifecycle' && !showLifecycle.checked) return false;

        return true;
      }

      function renderTimeline() {
        timelineEl.innerHTML = '';
        let count = 0;
        events.forEach(event => {
          if (!shouldShowEvent(event)) return;
          count += 1;

          const item = document.createElement('div');
          item.className = `trace-item trace-${event.event_type || 'text'}`;
          if (event.event_type === 'post_tool_use' && event.error) {
            item.classList.add('error');
          }

          const header = document.createElement('div');
          header.className = 'trace-header';

          const time = document.createElement('div');
          time.className = 'trace-time';
          time.textContent = formatTime(event.ts);

          const type = document.createElement('div');
          type.className = 'trace-type';
          type.textContent = formatTypeLabel(event.event_type);

          const summary = document.createElement('div');
          summary.className = 'trace-summary';
          const summaryParts = [];
          if (event.name) summaryParts.push(event.name);
          if (event.summary) summaryParts.push(event.summary);
          summary.textContent = summaryParts.join(' | ');

          header.appendChild(time);
          header.appendChild(type);
          header.appendChild(summary);

          const detail = document.createElement('div');
          detail.className = 'trace-detail';
          let detailText = event.details || '';
          if (showRaw.checked && event.raw) {
            detailText += (detailText ? '\\n\\n' : '') + '--- Raw JSON ---\\n' + event.raw;
          }
          detail.textContent = detailText || '(no details)';

          item.appendChild(header);
          item.appendChild(detail);

          header.addEventListener('click', () => {
            item.classList.toggle('expanded');
          });

          timelineEl.appendChild(item);
        });

        if (!count) {
          timelineEl.innerHTML = '<div class="empty">No events match the current filters.</div>';
        }
      }

      function renderTree() {
        treeEl.innerHTML = '';
        const grouped = {};
        events.forEach(event => {
          if (!shouldShowEvent(event)) return;
          const group = event.event_type || 'event';
          grouped[group] = grouped[group] || [];
          grouped[group].push(event);
        });

        const groupKeys = Object.keys(grouped).sort();
        if (!groupKeys.length) {
          treeEl.innerHTML = '<div class="empty">No events match the current filters.</div>';
          return;
        }

        groupKeys.forEach(groupKey => {
          const group = grouped[groupKey];
          const wrapper = document.createElement('div');
          wrapper.className = 'tree-agent';

          const header = document.createElement('div');
          header.className = 'tree-agent-header';

          const label = document.createElement('div');
          label.className = 'tree-agent-name';
          label.innerHTML = `<span class="expand-icon">▶</span>${formatTypeLabel(groupKey)}`;

          const stats = document.createElement('div');
          stats.className = 'tree-agent-stats';
          stats.textContent = `${group.length} events`;

          header.appendChild(label);
          header.appendChild(stats);

          const trace = document.createElement('div');
          trace.className = 'tree-agent-trace';

          group.forEach(event => {
            const item = document.createElement('div');
            item.className = `tree-item tree-item-${event.event_type || 'text'}`;
            if (event.event_type === 'post_tool_use' && event.error) {
              item.classList.add('error');
            }

            const time = document.createElement('div');
            time.className = 'tree-item-time';
            time.textContent = formatTime(event.ts);

            const labelText = document.createElement('span');
            labelText.className = 'tree-item-label';
            labelText.textContent = formatTypeLabel(event.event_type);

            const summary = document.createElement('div');
            summary.className = 'tree-item-content';
            const summaryParts = [];
            if (event.name) summaryParts.push(event.name);
            if (event.summary) summaryParts.push(event.summary);
            summary.textContent = summaryParts.join(' | ');

            const detail = document.createElement('div');
            detail.className = 'tree-item-content';
            detail.textContent = event.details ? event.details.substring(0, 500) : '';

            item.appendChild(time);
            item.appendChild(labelText);
            item.appendChild(summary);
            if (detail.textContent) item.appendChild(detail);

            trace.appendChild(item);
          });

          header.addEventListener('click', () => {
            wrapper.classList.toggle('expanded');
          });

          wrapper.appendChild(header);
          wrapper.appendChild(trace);
          treeEl.appendChild(wrapper);
        });
      }

      function render() {
        renderTimeline();
        renderTree();
      }

      [eventFilter, sourceFilter, searchInput, showUser, showThinking, showText, showTools, showLifecycle, showRaw]
        .forEach(control => control.addEventListener('change', render));
      searchInput.addEventListener('input', render);

      computeStats();
      render();
    } catch (err) {
      document.body.innerHTML = '<pre style="white-space: pre-wrap; color: #e94560;">Trace viewer error: ' + err + '</pre>';
      console.error(err);
    }
  </script>
</body>
</html>
"""

    def unique_token(*values):
        token = f"__CODEX_TOKEN_{uuid.uuid4().hex}__"
        while any(token in value for value in values):
            token = f"__CODEX_TOKEN_{uuid.uuid4().hex}__"
        return token

    events_token = unique_token(template, events_json, sessions_json)
    sessions_token = unique_token(template, events_json, sessions_json, events_token)
    html = (
        template.replace("__EVENTS_JSON__", events_token, 1)
        .replace("__SESSIONS_JSON__", sessions_token, 1)
        .replace("__PROJECT__", html_escape(session["cwd"] or "(unknown)"))
        .replace("__DURATION__", html_escape(session["duration"]))
        .replace("__MODEL__", html_escape(session["model"] or "(unknown)"))
        .replace("__CLI__", html_escape(session["cli_version"] or "(unknown)"))
        .replace("__SESSION_ID__", html_escape(session["id"]))
        .replace("__SESSION_SHORT__", html_escape(session["id"][:8]))
        .replace("__SOURCE_OPTIONS__", source_options)
    )
    return html.replace(events_token, events_json, 1).replace(
        sessions_token, sessions_json, 1
    )


def main():
    parser = argparse.ArgumentParser(
        description="Analyze Codex session logs",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("session_id", nargs="?", help="Session id or prefix")
    parser.add_argument("--list", action="store_true", help="List available sessions")
    parser.add_argument("--digest", action="store_true", help="Print session digest (markdown format)")
    parser.add_argument("--open", action="store_true", help="Open HTML viewer in browser")
    parser.add_argument("--limit", type=int, default=20, help="Limit list output")
    parser.add_argument("--latest", action="store_true", help="Analyze most recent session")
    parser.add_argument(
        "--sessions-dir",
        default=DEFAULT_SESSIONS_DIR,
        help=f"Sessions directory (default: {DEFAULT_SESSIONS_DIR})",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=0,
        help="Truncate event details to N characters (0 = no truncation)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Include raw JSON payloads in HTML output",
    )
    parser.add_argument(
        "--no-raw",
        action="store_true",
        help="Do not include raw JSON payloads (default)",
    )

    args = parser.parse_args()

    if not os.path.isdir(args.sessions_dir):
        print(f"Sessions directory not found: {args.sessions_dir}", file=sys.stderr)
        return 1

    include_raw = args.raw and not args.no_raw

    sessions = [
        parse_session(path, max_chars=args.max_chars, include_raw=include_raw)
        for path in iter_session_files(args.sessions_dir)
    ]

    if args.list:
        print_list(sessions, limit=args.limit)
        return 0

    # Handle --latest flag or require session_id
    if args.latest:
        sessions_sorted = sorted(sessions, key=lambda s: s["start_ts"] or dt.datetime.min, reverse=True)
        if not sessions_sorted:
            print("No sessions found", file=sys.stderr)
            return 1
        session = sessions_sorted[0]
    elif args.session_id:
        session, matches = select_session(sessions, args.session_id)
        if not session:
            print(f"No unique match for '{args.session_id}'. Matches: {len(matches)}")
            for match in matches[:10]:
                print(f"- {match['id']} ({match['path']})")
            return 1
    else:
        parser.print_help()
        return 1

    if args.open:
        html = build_html(session, sessions)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(html)
        path = handle.name
        print(f"Opening {path}", file=sys.stderr)
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([opener, path], check=False)
        return 0

    if args.digest:
        try:
            print(generate_digest(session))
        except BrokenPipeError:
            return 0
        return 0

    # Default: show HTML
    html = build_html(session, sessions)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(html)
    path = handle.name
    print(f"Opening {path}", file=sys.stderr)
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.run([opener, path], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
