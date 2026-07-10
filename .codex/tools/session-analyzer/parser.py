#!/usr/bin/env python3
"""Inspect Codex session history, traces, agents, tokens, and estimated cost.

Codex stores one rollout JSONL per thread. Sub-agents are separate rollouts linked
to their parent through ``session_meta.payload.parent_thread_id``. This analyzer
reconstructs that topology, while remaining defensive about the internal JSONL
schema (unknown records are retained as generic lifecycle events).
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
DEFAULT_SESSIONS_DIR = DEFAULT_CODEX_HOME / "sessions"
DEFAULT_ARCHIVED_DIR = DEFAULT_CODEX_HOME / "archived_sessions"

# Public API list prices in USD per 1M tokens. These are estimates for API-style
# billing, not authoritative ChatGPT/Codex subscription charges. Deliberately do
# not guess a price for private/internal model slugs (for example gpt-5.6-sol or
# codex-auto-review); those render as N/A until a public rate exists.
PRICING_USD_PER_MTOK = {
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.3-codex": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
    "gpt-5.2-codex": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
    "gpt-5.2": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
    "gpt-5.1-codex": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5.1": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-codex": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "codex-mini-latest": {"input": 1.50, "cached_input": 0.375, "output": 6.00},
}

USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

SKIP_SUBSTRINGS = (
    "<environment_context>",
    "<permissions instructions>",
    "<skills_instructions>",
    "<app-context>",
    "<recommended_plugins>",
    "# agents.md instructions",
    "skills are discovered",
    "trigger rules",
)

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

TOOL_CALL_TYPES = {
    "function_call",
    "custom_tool_call",
    "web_search_call",
    "tool_search_call",
}
TOOL_OUTPUT_TYPES = {
    "function_call_output",
    "custom_tool_call_output",
    "tool_search_output",
}


def parse_ts(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        value = str(value)
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def format_ts(ts):
    return ts.isoformat() if ts else ""


def format_duration(start_ts, end_ts):
    if not start_ts or not end_ts:
        return "?"
    seconds = max((end_ts - start_ts).total_seconds(), 0)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def empty_usage():
    return {field: 0 for field in USAGE_FIELDS}


def normalize_usage(value):
    result = empty_usage()
    if not isinstance(value, dict):
        return result
    for field in USAGE_FIELDS:
        number = value.get(field)
        if isinstance(number, (int, float)) and number >= 0:
            result[field] = number
    if not result["total_tokens"]:
        # Codex's input/output totals already include cached/reasoning subsets.
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def add_usage(left, right):
    return {
        field: (left or {}).get(field, 0) + (right or {}).get(field, 0)
        for field in USAGE_FIELDS
    }


def usage_delta(current, previous, fallback=None):
    """Return the positive delta between cumulative token snapshots."""
    current = normalize_usage(current)
    previous = normalize_usage(previous)
    if all(current[field] >= previous[field] for field in USAGE_FIELDS):
        return {field: current[field] - previous[field] for field in USAGE_FIELDS}
    return normalize_usage(fallback)


def usage_has_tokens(usage):
    return any((usage or {}).get(field, 0) for field in USAGE_FIELDS)


def public_model_key(model):
    model = (model or "").lower()
    if model in PRICING_USD_PER_MTOK:
        return model
    # Public date snapshots keep the base model prefix. Match longest first so
    # gpt-5.4-mini-* does not accidentally match gpt-5.4.
    for key in sorted(PRICING_USD_PER_MTOK, key=len, reverse=True):
        if model.startswith(key + "-") and re.search(r"-20\d{2}-\d{2}-\d{2}$", model):
            return key
    return None


def estimate_cost(model, usage):
    """Estimate cost without double-counting cached or reasoning subsets."""
    usage = normalize_usage(usage)
    if not usage_has_tokens(usage):
        return None
    key = public_model_key(model)
    if not key:
        return None
    rates = PRICING_USD_PER_MTOK[key]
    cached = min(usage["cached_input_tokens"], usage["input_tokens"])
    uncached = max(usage["input_tokens"] - cached, 0)
    # output_tokens already includes reasoning_output_tokens.
    return (
        uncached * rates["input"]
        + cached * rates["cached_input"]
        + usage["output_tokens"] * rates["output"]
    ) / 1_000_000.0


def estimate_model_usage(model_usage):
    total = 0.0
    unknown = []
    for model, usage in (model_usage or {}).items():
        if not usage_has_tokens(usage):
            continue
        cost = estimate_cost(model, usage)
        if cost is None:
            unknown.append(model or "unknown")
        else:
            total += cost
    return (None if unknown else total), sorted(set(unknown))


def format_cost_usd(cost):
    return "N/A" if cost is None else f"${cost:.4f}"


def format_tokens(number):
    number = number or 0
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return f"{number:g}" if isinstance(number, float) else str(number)


def extract_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("input_text", "output_text", "text"):
            parts.append(item.get("text") or "")
    return "".join(parts)


def is_instruction_text(text):
    if not text:
        return True
    lower = text.lower()
    return any(marker in lower for marker in SKIP_SUBSTRINGS)


def summarize_text(text, max_len=80):
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return "(no summary)"
    return cleaned if len(cleaned) <= max_len else cleaned[: max_len - 3] + "..."


def truncate_text(text, max_chars):
    if not text:
        return ""
    if not isinstance(text, str):
        text = stringify_details(text)
    if max_chars and len(text) > max_chars:
        return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"
    return text


def stringify_details(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def maybe_parse_json(value):
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass
    return value


def format_tool_args(args):
    parsed = maybe_parse_json(args)
    if not isinstance(parsed, dict):
        return stringify_details(parsed)
    preferred = (
        "cmd",
        "command",
        "url",
        "file_path",
        "path",
        "uri",
        "workdir",
        "timeout_ms",
        "justification",
    )
    keys = sorted(
        parsed,
        key=lambda key: (
            key not in preferred,
            preferred.index(key) if key in preferred else 999,
            key,
        ),
    )
    return "\n".join(f"{key}: {stringify_details(parsed[key])}" for key in keys)


def summarize_tool_args(args):
    parsed = maybe_parse_json(args)
    if isinstance(parsed, dict):
        for key in ("cmd", "command", "url", "file_path", "path", "uri"):
            if key in parsed:
                return f"{key}={summarize_text(str(parsed[key]), 110)}"
    return summarize_text(stringify_details(parsed), 120)


def summarize_tool_output(output):
    parsed = maybe_parse_json(output)
    if isinstance(parsed, dict):
        pieces = []
        exit_code = parsed.get("exit_code")
        if exit_code is not None:
            pieces.append(f"exit={exit_code}")
        status = parsed.get("status")
        if status:
            pieces.append(str(status))
        body = parsed.get("output") or parsed.get("result") or parsed.get("error")
        if body:
            pieces.append(summarize_text(stringify_details(body), 120))
        return " ".join(pieces) or summarize_text(stringify_details(parsed), 120)
    text = stringify_details(parsed)
    exit_match = re.search(r"(?:Exit code:|exited with code)\s*(\d+)", text, re.I)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    pieces = [f"exit={exit_match.group(1)}"] if exit_match else []
    if lines:
        pieces.append(lines[0][:120])
    return " ".join(pieces) or "(no output)"


def tool_output_error(output):
    parsed = maybe_parse_json(output)
    if isinstance(parsed, dict):
        if parsed.get("is_error") is True or parsed.get("success") is False:
            return True
        if parsed.get("error") and not parsed.get("output"):
            return True
        exit_code = parsed.get("exit_code")
        if exit_code is not None:
            return str(exit_code) != "0"
        if str(parsed.get("status", "")).lower() in ("error", "failed", "failure"):
            return True
    text = stringify_details(parsed)
    match = re.search(r"(?:Exit code:|exited with code)\s*(\d+)", text, re.I)
    return bool(match and match.group(1) != "0")


def extract_reasoning_text(payload):
    parts = []
    summary = payload.get("summary")
    if isinstance(summary, list):
        for item in summary:
            if isinstance(item, dict):
                parts.append(item.get("summary_text") or item.get("text") or "")
            elif isinstance(item, str):
                parts.append(item)
    if payload.get("content"):
        parts.append(stringify_details(payload["content"]))
    if payload.get("encrypted_content") and not parts:
        parts.append("(encrypted reasoning content)")
    return "\n".join(part for part in parts if part)


def iter_session_files(sessions_dir, archived_dir=None):
    """Yield active and archived rollout files exactly once."""
    seen = set()
    roots = [Path(sessions_dir).expanduser()]
    if archived_dir:
        roots.append(Path(archived_dir).expanduser())
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("rollout-*.jsonl"):
            key = str(path.resolve())
            if key not in seen:
                seen.add(key)
                yield str(path)


def read_jsonl(path):
    records = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    record["_line_index"] = index
                    records.append(record)
    except OSError:
        return []
    return records


def format_session_meta(payload):
    keys = (
        "id",
        "parent_thread_id",
        "cwd",
        "originator",
        "cli_version",
        "model_provider",
        "thread_source",
        "source",
    )
    return "\n".join(f"{key}: {payload[key]}" for key in keys if key in payload)


def format_turn_context(payload):
    lines = []
    for key in ("cwd", "model", "effort", "approval_policy", "collaboration_mode"):
        if payload.get(key) is not None:
            lines.append(f"{key}: {payload[key]}")
    sandbox = payload.get("sandbox_policy") or payload.get("file_system_sandbox_policy") or {}
    if isinstance(sandbox, dict) and sandbox.get("type"):
        lines.append(f"sandbox: {sandbox['type']}")
    return "\n".join(lines)


def format_token_count(payload):
    info = payload.get("info") if isinstance(payload, dict) else None
    info = info or {}
    lines = []
    for label, usage in (
        ("total", info.get("total_token_usage")),
        ("last", info.get("last_token_usage")),
    ):
        usage = normalize_usage(usage)
        if usage_has_tokens(usage):
            lines.append(
                label
                + ": "
                + ", ".join(f"{field}={usage[field]}" for field in USAGE_FIELDS)
            )
    if info.get("model_context_window") is not None:
        lines.append(f"context_window={info['model_context_window']}")
    return "\n".join(lines)


def agent_identity(meta):
    source = meta.get("source")
    parent_id = meta.get("parent_thread_id")
    label = "main"
    role = "main"
    depth = 0
    if isinstance(source, dict) and isinstance(source.get("subagent"), dict):
        subagent = source["subagent"]
        spawn = subagent.get("thread_spawn")
        if isinstance(spawn, dict):
            parent_id = parent_id or spawn.get("parent_thread_id")
            label = spawn.get("agent_nickname") or spawn.get("agent_role") or "subagent"
            role = spawn.get("agent_role") or "subagent"
            depth = spawn.get("depth") or 1
        else:
            label = subagent.get("other") or "subagent"
            role = subagent.get("other") or "subagent"
            depth = 1
    elif meta.get("thread_source") == "subagent" or parent_id:
        label = "subagent"
        role = "subagent"
        depth = 1
    return {
        "parent_id": parent_id,
        "agent_label": str(label),
        "agent_role": str(role),
        "agent_depth": depth,
        "is_subagent": bool(parent_id or role != "main"),
    }


def parse_session(path, max_chars=0, include_raw=False):
    records = read_jsonl(path)
    meta = {}
    for record in records:
        if record.get("type") == "session_meta":
            meta = record.get("payload") or {}
            break

    identity = agent_identity(meta)
    tool_counts = collections.Counter()
    user_messages = []
    assistant_messages = []
    first_ts = None
    last_ts = None
    model = None
    cli_version = meta.get("cli_version")
    token_usage = empty_usage()
    model_usage = collections.defaultdict(empty_usage)
    previous_usage = empty_usage()
    events = []
    call_names = {}

    # event_msg carries the canonical visible messages. response_item mirrors
    # those messages but also contains injected system/developer context.
    canonical_messages = collections.Counter()
    for record in records:
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload") or {}
        if payload.get("type") == "user_message":
            canonical_messages[("user", payload.get("message") or "")] += 1
        elif payload.get("type") == "agent_message":
            canonical_messages[("assistant", payload.get("message") or "")] += 1

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
        events.append(
            {
                "index": len(events),
                "ts": format_ts(ts),
                "epoch": ts.timestamp() if ts else 0,
                "category": category,
                "source": source,
                "kind": kind,
                "role": role,
                "name": name,
                "summary": summary,
                "details": truncate_text(details, max_chars),
                "raw": raw,
                "event_type": event_type,
                "error": bool(error),
                "agent_id": meta.get("id") or Path(path).stem,
                "agent_label": identity["agent_label"],
                "agent_role": identity["agent_role"],
            }
        )

    for record in records:
        ts = parse_ts(record.get("timestamp"))
        if ts:
            first_ts = min(first_ts, ts) if first_ts else ts
            last_ts = max(last_ts, ts) if last_ts else ts
        raw_json = ""
        if include_raw:
            raw_copy = {key: value for key, value in record.items() if key != "_line_index"}
            raw_json = json.dumps(raw_copy, indent=2, ensure_ascii=False)

        outer_type = record.get("type")
        payload = record.get("payload") or {}

        if outer_type == "session_meta":
            add_event(
                ts,
                CATEGORY_LIFECYCLE,
                "session_meta",
                "session_meta",
                summary=f"session_meta id={(meta.get('id') or '')[:8]}",
                details=format_session_meta(meta),
                raw=raw_json,
                event_type=EVENT_LIFECYCLE,
            )
            continue

        if outer_type == "turn_context":
            model = payload.get("model") or model
            add_event(
                ts,
                CATEGORY_LIFECYCLE,
                "turn_context",
                "turn_context",
                summary=f"turn_context model={model or 'unknown'}",
                details=format_turn_context(payload),
                raw=raw_json,
                event_type=EVENT_LIFECYCLE,
            )
            continue

        if outer_type == "response_item":
            item_type = payload.get("type")
            if item_type == "message":
                role = payload.get("role") or ""
                text = extract_text(payload.get("content"))
                key = (role, text)
                if canonical_messages.get(key, 0):
                    canonical_messages[key] -= 1
                    continue
                if role == "user" and is_instruction_text(text):
                    continue
                if role == "user":
                    category, event_type = CATEGORY_USER, EVENT_USER
                    user_messages.append(text)
                elif role == "assistant":
                    category, event_type = CATEGORY_ASSISTANT, EVENT_TEXT
                    assistant_messages.append(text)
                else:
                    category, event_type = CATEGORY_OTHER, EVENT_TEXT
                add_event(
                    ts,
                    category,
                    "response_item",
                    "message",
                    role=role,
                    summary=summarize_text(text, 120),
                    details=text,
                    raw=raw_json,
                    event_type=event_type,
                )
            elif item_type in TOOL_CALL_TYPES:
                name = payload.get("name") or (
                    "web_search" if item_type == "web_search_call" else "tool_search"
                )
                args = payload.get("arguments")
                if args is None:
                    args = payload.get("input")
                if args is None:
                    args = payload.get("action") or payload
                call_id = payload.get("call_id") or payload.get("id")
                if call_id:
                    call_names[call_id] = name
                tool_counts[name] += 1
                arg_summary = summarize_tool_args(args)
                add_event(
                    ts,
                    CATEGORY_TOOL,
                    "response_item",
                    item_type,
                    name=name,
                    summary=f"{name}: {arg_summary}" if arg_summary else name,
                    details=format_tool_args(args),
                    raw=raw_json,
                    event_type=EVENT_TOOL_USE,
                    error=str(payload.get("status", "")).lower() in ("failed", "error"),
                )
            elif item_type in TOOL_OUTPUT_TYPES:
                output = payload.get("output") or payload.get("result") or ""
                call_id = payload.get("call_id") or payload.get("id") or ""
                name = call_names.get(call_id, call_id)
                add_event(
                    ts,
                    CATEGORY_TOOL,
                    "response_item",
                    item_type,
                    name=name,
                    summary=summarize_tool_output(output),
                    details=stringify_details(output),
                    raw=raw_json,
                    event_type=EVENT_POST_TOOL,
                    error=tool_output_error(output),
                )
            elif item_type == "reasoning":
                text = extract_reasoning_text(payload)
                add_event(
                    ts,
                    CATEGORY_THINKING,
                    "response_item",
                    "reasoning",
                    summary=summarize_text(text or "reasoning", 120),
                    details=text,
                    raw=raw_json,
                    event_type=EVENT_THINKING,
                )
            else:
                add_event(
                    ts,
                    CATEGORY_LIFECYCLE,
                    "response_item",
                    item_type or "response_item",
                    summary=item_type or "response_item",
                    details=summarize_text(stringify_details(payload), 240),
                    raw=raw_json,
                    event_type=EVENT_LIFECYCLE,
                )
            continue

        if outer_type == "event_msg":
            item_type = payload.get("type") or "event_msg"
            if item_type == "user_message":
                text = payload.get("message") or ""
                if not is_instruction_text(text):
                    user_messages.append(text)
                    add_event(
                        ts,
                        CATEGORY_USER,
                        "event_msg",
                        item_type,
                        role="user",
                        summary=summarize_text(text, 120),
                        details=text,
                        raw=raw_json,
                        event_type=EVENT_USER,
                    )
            elif item_type == "agent_message":
                text = payload.get("message") or ""
                assistant_messages.append(text)
                add_event(
                    ts,
                    CATEGORY_ASSISTANT,
                    "event_msg",
                    item_type,
                    role="assistant",
                    summary=summarize_text(text, 120),
                    details=text,
                    raw=raw_json,
                    event_type=EVENT_TEXT,
                )
            elif item_type == "agent_reasoning":
                text = payload.get("text") or ""
                add_event(
                    ts,
                    CATEGORY_THINKING,
                    "event_msg",
                    item_type,
                    summary=summarize_text(text or item_type, 120),
                    details=text,
                    raw=raw_json,
                    event_type=EVENT_THINKING,
                )
            elif item_type == "token_count":
                info = payload.get("info") or {}
                total = normalize_usage(info.get("total_token_usage"))
                last = normalize_usage(info.get("last_token_usage"))
                if usage_has_tokens(total):
                    delta = usage_delta(total, previous_usage, last)
                    if usage_has_tokens(delta):
                        active_model = model or "unknown"
                        model_usage[active_model] = add_usage(model_usage[active_model], delta)
                    token_usage = total
                    previous_usage = total
                add_event(
                    ts,
                    CATEGORY_LIFECYCLE,
                    "event_msg",
                    item_type,
                    summary=f"token_count total={token_usage.get('total_tokens', 0)}",
                    details=format_token_count(payload),
                    raw=raw_json,
                    event_type=EVENT_LIFECYCLE,
                )
            else:
                category = CATEGORY_TOOL if item_type in (
                    "patch_apply_end",
                    "mcp_tool_call_end",
                    "web_search_end",
                ) else CATEGORY_LIFECYCLE
                event_type = EVENT_POST_TOOL if category == CATEGORY_TOOL else EVENT_LIFECYCLE
                add_event(
                    ts,
                    category,
                    "event_msg",
                    item_type,
                    summary=item_type,
                    details=summarize_text(stringify_details(payload), 300),
                    raw=raw_json,
                    event_type=event_type,
                    error=str(payload.get("status", "")).lower() in ("failed", "error"),
                )
            continue

        # Newer clients may add outer record families (for example world_state).
        add_event(
            ts,
            CATEGORY_LIFECYCLE,
            outer_type or "record",
            outer_type or "record",
            summary=outer_type or "record",
            details="keys=" + ", ".join(sorted(payload)) if isinstance(payload, dict) else "",
            raw=raw_json,
            event_type=EVENT_LIFECYCLE,
        )

    if not usage_has_tokens(token_usage) and model_usage:
        for usage in model_usage.values():
            token_usage = add_usage(token_usage, usage)
    if usage_has_tokens(token_usage) and not model_usage:
        model_usage[model or "unknown"] = token_usage

    session_id = meta.get("id") or Path(path).stem.replace("rollout-", "")
    for event in events:
        event["agent_id"] = session_id
    events.sort(key=lambda event: (event["epoch"], event["index"]))
    counts_by_category = collections.Counter(event["category"] for event in events)
    counts_by_kind = collections.Counter(event["kind"] for event in events)
    counts_by_source = collections.Counter(event["source"] for event in events)
    counts_by_event = collections.Counter(event["event_type"] for event in events)
    estimated_cost, unknown_cost_models = estimate_model_usage(model_usage)

    return {
        "id": session_id,
        "parent_id": identity["parent_id"],
        "is_subagent": identity["is_subagent"],
        "agent_label": identity["agent_label"],
        "agent_role": identity["agent_role"],
        "agent_depth": identity["agent_depth"],
        "path": str(path),
        "archived": "archived_sessions" in Path(path).parts,
        "cwd": meta.get("cwd"),
        "start_ts": first_ts,
        "end_ts": last_ts,
        "duration": format_duration(first_ts, last_ts),
        "user_messages": user_messages,
        "assistant_messages": assistant_messages,
        "tool_counts": tool_counts,
        "model": model,
        "cli_version": cli_version,
        "turn_count": counts_by_kind.get("task_started", 0),
        "token_usage": token_usage,
        "model_usage": dict(model_usage),
        "estimated_cost": estimated_cost,
        "unknown_cost_models": unknown_cost_models,
        "summary": summarize_text(next((m for m in user_messages if not is_instruction_text(m)), "")),
        "events": events,
        "counts_by_category": counts_by_category,
        "counts_by_kind": counts_by_kind,
        "counts_by_source": counts_by_source,
        "counts_by_event": counts_by_event,
        "meta": meta,
    }


def session_sort_key(session):
    value = session.get("start_ts")
    if value:
        try:
            return value.timestamp()
        except (OSError, ValueError):
            pass
    try:
        return Path(session["path"]).stat().st_mtime
    except OSError:
        return 0


def same_project(session, project):
    if not project:
        return True
    cwd = session.get("cwd")
    if not cwd:
        return False
    return os.path.realpath(os.path.expanduser(cwd)) == os.path.realpath(os.path.expanduser(project))


def select_session(sessions, needle):
    if not needle:
        return None, []
    expanded = str(Path(needle).expanduser())
    matches = []
    for session in sessions:
        if session["id"].startswith(needle):
            matches.append(session)
        elif needle in session["path"] or expanded == session["path"]:
            matches.append(session)
    return (matches[0] if len(matches) == 1 else None), matches


def descendants(root, sessions):
    children = collections.defaultdict(list)
    for session in sessions:
        if session.get("parent_id"):
            children[session["parent_id"]].append(session)
    result = []
    queue = list(sorted(children[root["id"]], key=session_sort_key))
    seen = {root["id"]}
    while queue:
        child = queue.pop(0)
        if child["id"] in seen:
            continue
        seen.add(child["id"])
        result.append(child)
        queue.extend(sorted(children[child["id"]], key=session_sort_key))
    return result


def combine_thread(root, sessions):
    members = [root] + descendants(root, sessions)
    events = []
    tool_counts = collections.Counter()
    usage = empty_usage()
    model_usage = collections.defaultdict(empty_usage)
    starts = []
    ends = []
    agents = []
    for index, member in enumerate(members):
        label = "main" if index == 0 else member["agent_label"]
        agents.append(
            {
                "id": member["id"],
                "parent_id": member.get("parent_id"),
                "label": label,
                "role": member["agent_role"],
                "depth": member["agent_depth"],
                "model": member.get("model"),
                "duration": member["duration"],
                "turn_count": member.get("turn_count", 0),
                "token_usage": member["token_usage"],
                "estimated_cost": member["estimated_cost"],
                "unknown_cost_models": member["unknown_cost_models"],
                "path": member["path"],
            }
        )
        for event in member["events"]:
            copied = dict(event)
            copied["agent_label"] = label
            copied["agent_role"] = member["agent_role"]
            events.append(copied)
        tool_counts.update(member["tool_counts"])
        usage = add_usage(usage, member["token_usage"])
        for model, model_tokens in member["model_usage"].items():
            model_usage[model] = add_usage(model_usage[model], model_tokens)
        if member.get("start_ts"):
            starts.append(member["start_ts"])
        if member.get("end_ts"):
            ends.append(member["end_ts"])

    events.sort(key=lambda event: (event["epoch"], event["agent_id"], event["index"]))
    estimated_cost, unknown_cost_models = estimate_model_usage(model_usage)
    combined = dict(root)
    combined.update(
        {
            "start_ts": min(starts) if starts else root.get("start_ts"),
            "end_ts": max(ends) if ends else root.get("end_ts"),
            "duration": format_duration(min(starts), max(ends)) if starts and ends else root["duration"],
            "events": events,
            "tool_counts": tool_counts,
            "token_usage": usage,
            "model_usage": dict(model_usage),
            "estimated_cost": estimated_cost,
            "unknown_cost_models": unknown_cost_models,
            "agents": agents,
            "agent_count": len(agents),
            "turn_count": sum(member.get("turn_count", 0) for member in members),
            "counts_by_category": collections.Counter(e["category"] for e in events),
            "counts_by_kind": collections.Counter(e["kind"] for e in events),
            "counts_by_source": collections.Counter(e["source"] for e in events),
            "counts_by_event": collections.Counter(e["event_type"] for e in events),
        }
    )
    return combined


def serializable(value):
    if isinstance(value, dt.datetime):
        return format_ts(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, collections.Counter):
        return dict(value)
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items() if key != "meta"}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    return value


def session_list_record(session):
    """Compact, stable JSON shape for --list --output json."""
    return {
        "id": session["id"],
        "parent_id": session.get("parent_id"),
        "is_subagent": session.get("is_subagent", False),
        "archived": session.get("archived", False),
        "path": session["path"],
        "cwd": session.get("cwd"),
        "start_ts": format_ts(session.get("start_ts")),
        "end_ts": format_ts(session.get("end_ts")),
        "duration": session.get("duration"),
        "summary": session.get("summary"),
        "model": session.get("model"),
        "agent_count": session.get("agent_count", 1),
        "turn_count": session.get("turn_count", 0),
        "token_usage": session.get("token_usage", empty_usage()),
        "model_usage": session.get("model_usage", {}),
        "estimated_cost": session.get("estimated_cost"),
        "unknown_cost_models": session.get("unknown_cost_models", []),
    }


def print_list(sessions, limit=20):
    sessions = sorted(sessions, key=session_sort_key, reverse=True)
    if limit:
        sessions = sessions[:limit]
    print("ID        Duration Agents     Tokens  Est.Cost  Model              Project                    Summary")
    print("-" * 132)
    for session in sessions:
        project = session.get("cwd") or "(unknown)"
        if len(project) > 26:
            project = "..." + project[-23:]
        model = session.get("model") or "unknown"
        if len(model) > 18:
            model = model[:17] + "…"
        print(
            f"{session['id'][:8]:<10}{session['duration']:>8}"
            f"{session.get('agent_count', 1):>7}"
            f"{format_tokens(session['token_usage'].get('total_tokens', 0)):>11}"
            f"{format_cost_usd(session.get('estimated_cost')):>10}  "
            f"{model:<18} {project:<26} {session.get('summary') or '(no summary)'}"
        )


def usage_breakdown(usage):
    usage = normalize_usage(usage)
    return (
        f"in {usage['input_tokens']:,} (cached {usage['cached_input_tokens']:,}) · "
        f"out {usage['output_tokens']:,} (reasoning {usage['reasoning_output_tokens']:,})"
    )


def build_overview(root, sessions):
    combined = combine_thread(root, sessions)
    return {
        "session_id": root["id"],
        "transcript": root["path"],
        "project": root.get("cwd"),
        "duration": combined["duration"],
        "main": combined["agents"][0],
        "subagents": combined["agents"][1:],
        "session_total": {
            "agent_count": combined["agent_count"],
            "turn_count": combined["turn_count"],
            "token_usage": combined["token_usage"],
            "model_usage": combined["model_usage"],
            "estimated_cost": combined["estimated_cost"],
            "unknown_cost_models": combined["unknown_cost_models"],
        },
    }


def format_overview(overview):
    main = overview["main"]
    total = overview["session_total"]
    lines = [
        "CODEX SESSION OVERVIEW",
        "=" * 72,
        f"Session:    {overview['session_id']}",
        f"Project:    {overview['project'] or '(unknown)'}",
        f"Transcript: {overview['transcript']}",
        f"Duration:   {overview['duration']}",
        "",
        "MAIN AGENT",
        "=" * 72,
        f"  {main['model'] or 'unknown'} · {main['turn_count']} turns · "
        f"{main['token_usage']['total_tokens']:,} tokens · est. {format_cost_usd(main['estimated_cost'])}",
        f"  {usage_breakdown(main['token_usage'])}",
    ]
    if overview["subagents"]:
        lines.extend(["", f"SUB-AGENTS ({len(overview['subagents'])})", "=" * 72])
        for agent in overview["subagents"]:
            indent = "  " * max(agent.get("depth", 1), 1)
            lines.append(
                f"{indent}{agent['label']} [{agent['role']}] {agent['id'][:12]} · "
                f"{agent['model'] or 'unknown'} · {agent['token_usage']['total_tokens']:,} tokens · "
                f"est. {format_cost_usd(agent['estimated_cost'])}"
            )
            lines.append(f"{indent}  {usage_breakdown(agent['token_usage'])}")
            lines.append(f"{indent}  {agent['path']}")
    lines.extend(
        [
            "",
            "SESSION TOTAL (main + descendants)",
            "=" * 72,
            f"  {total['agent_count']} agents · {total['turn_count']} turns · "
            f"{total['token_usage']['total_tokens']:,} tokens · est. "
            f"{format_cost_usd(total['estimated_cost'])}",
            f"  {usage_breakdown(total['token_usage'])}",
            "",
            "  MODEL BREAKDOWN",
        ]
    )
    for model, usage in sorted(
        total["model_usage"].items(),
        key=lambda pair: pair[1].get("total_tokens", 0),
        reverse=True,
    ):
        lines.append(
            f"    {model}: {usage['total_tokens']:,} tokens · "
            f"est. {format_cost_usd(estimate_cost(model, usage))} · {usage_breakdown(usage)}"
        )
    if total["unknown_cost_models"]:
        lines.extend(
            [
                "",
                "  Cost is N/A because no public API price is available for: "
                + ", ".join(total["unknown_cost_models"]),
            ]
        )
    lines.extend(
        [
            "",
            "  Token totals come from each rollout's final cumulative token_count event.",
            "  Cached input and reasoning output are subsets, not extra tokens. Cost is an",
            "  API-rate estimate only; provider billing/subscription usage is authoritative.",
        ]
    )
    return "\n".join(lines)


def generate_digest(session):
    lines = [
        f"# Codex Session Digest: {session['id'][:8]}",
        "",
        f"**Project:** {session.get('cwd') or '(unknown)'}",
        f"**Duration:** {session['duration']}",
        f"**Agents:** {session.get('agent_count', 1)}",
        f"**Models:** {', '.join(session.get('model_usage', {})) or session.get('model') or 'unknown'}",
        "",
        "## Token & Cost",
        "",
        f"- Total: {session['token_usage']['total_tokens']:,} tokens",
        f"- {usage_breakdown(session['token_usage'])}",
        f"- Estimated cost: {format_cost_usd(session.get('estimated_cost'))}",
    ]
    if session.get("unknown_cost_models"):
        lines.append(
            "- Cost unavailable for unpriced model(s): "
            + ", ".join(session["unknown_cost_models"])
        )
    if session.get("agents"):
        lines.extend(["", "### By agent", ""])
        for agent in session["agents"]:
            lines.append(
                f"- **{agent['label']}** ({agent['model'] or 'unknown'}): "
                f"{agent['token_usage']['total_tokens']:,} tokens · "
                f"est. {format_cost_usd(agent['estimated_cost'])}"
            )
    if session.get("model_usage"):
        lines.extend(["", "### By model", ""])
        for model, usage in sorted(
            session["model_usage"].items(),
            key=lambda pair: pair[1].get("total_tokens", 0),
            reverse=True,
        ):
            lines.append(
                f"- **{model}**: {usage['total_tokens']:,} tokens · "
                f"est. {format_cost_usd(estimate_cost(model, usage))}"
            )

    prompts = [message for message in session["user_messages"] if not is_instruction_text(message)]
    if prompts:
        lines.extend(["", "## User Prompts (chronological)", ""])
        for index, text in enumerate(prompts[:10], 1):
            lines.append(f'{index}. "{summarize_text(text, 300)}"')

    thinking = [event for event in session["events"] if event["event_type"] == EVENT_THINKING]
    if thinking:
        lines.extend(["", "## Key Thinking/Decisions", ""])
        for event in sorted(thinking, key=lambda item: len(item.get("details") or ""), reverse=True)[:5]:
            lines.append(f"- {summarize_text(event.get('details') or event.get('summary'), 400)}")

    commands = []
    files = []
    for event in session["events"]:
        if event["event_type"] != EVENT_TOOL_USE:
            continue
        details = event.get("details") or ""
        command = re.search(r"(?:cmd|command):\s*(.+?)(?:\n|$)", details, re.I)
        if command and command.group(1) not in commands:
            commands.append(command.group(1)[:160])
        for key in ("file_path", "path"):
            match = re.search(rf"{key}:\s*(.+?)(?:\n|$)", details, re.I)
            if match and match.group(1) not in files:
                files.append(match.group(1))
    if commands:
        lines.extend(["", "## Commands Run", ""])
        lines.extend(f"- `{command}`" for command in commands[:15])
    if files:
        lines.extend(["", "## Files Referenced/Modified", ""])
        lines.extend(f"- {path}" for path in files[:15])

    errors = [
        event
        for event in session["events"]
        if event["event_type"] == EVENT_POST_TOOL and event.get("error")
    ]
    if errors:
        lines.extend(["", "## Errors Encountered", ""])
        for event in errors[:5]:
            lines.append(f"- **{event.get('name') or 'tool'}**: {event.get('summary') or ''}")

    lines.extend(["", "## Where It Left Off", ""])
    if prompts:
        lines.extend(["**Last user request:**", f"> {prompts[-1][:500]}", ""])
    responses = [event for event in session["events"] if event["event_type"] == EVENT_TEXT]
    if responses:
        text = (responses[-1].get("details") or responses[-1].get("summary") or "")[:500]
        lines.extend(["**Last assistant response:**", "> " + text.replace("\n", "\n> "), ""])
    return "\n".join(lines)


def build_html(session, sessions):
    events_json = json.dumps(session["events"], ensure_ascii=False).replace("</", "<\\/")
    session_rows = [
        {
            "id": item["id"],
            "summary": item.get("summary"),
            "duration": item.get("duration"),
        }
        for item in sessions
        if not item.get("is_subagent")
    ]
    sessions_json = json.dumps(session_rows, ensure_ascii=False).replace("</", "<\\/")
    agents = session.get("agents") or [
        {"id": session["id"], "label": "main", "role": "main"}
    ]
    agent_options = "".join(
        f'<option value="{html.escape(agent["id"])}">'
        f'{html.escape(agent["label"])} ({html.escape(agent["role"])})</option>'
        for agent in agents
    )
    usage = session["token_usage"]
    project = html.escape(session.get("cwd") or "(unknown)")
    model_mix = html.escape(", ".join(session.get("model_usage", {})) or session.get("model") or "unknown")
    cost = html.escape(format_cost_usd(session.get("estimated_cost")))
    unknown_note = ""
    if session.get("unknown_cost_models"):
        unknown_note = (
            '<div class="cost-note">No public rate for '
            + html.escape(", ".join(session["unknown_cost_models"]))
            + "; cost shown as N/A.</div>"
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Session {html.escape(session['id'][:8])}</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;background:#111827;color:#e5e7eb;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.header{{padding:22px 28px;background:#172033;border-bottom:1px solid #334155;position:sticky;top:0;z-index:2}}
.top{{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}} h1{{margin:0 0 5px;font-size:22px}}
.meta{{color:#94a3b8;font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px}} select,input{{background:#0f172a;color:#e5e7eb;border:1px solid #475569;border-radius:6px;padding:7px}}
.stats{{display:flex;flex-wrap:wrap;gap:10px;margin-top:15px}} .stat{{background:#0f3460;padding:8px 13px;border-radius:7px;min-width:95px}}
.stat b{{display:block;color:#f472b6;font-size:18px}} .stat span{{color:#bfdbfe;font-size:11px;text-transform:uppercase}} .cost-note{{color:#fbbf24;margin-top:8px;font-size:12px}}
.filters{{display:flex;flex-wrap:wrap;gap:9px;align-items:center;padding:13px 28px;background:#16213e;border-bottom:1px solid #334155;position:sticky;top:151px;z-index:2}}
.filters input[type=text]{{min-width:240px}} .filters label{{color:#cbd5e1;font-size:12px}} .tabs{{padding:12px 28px 0}}
.tab{{background:#1e293b;color:#cbd5e1;border:1px solid #475569;padding:7px 12px;border-radius:6px;cursor:pointer}} .tab.active{{background:#0f3460;color:white}}
.view{{display:none;padding:18px 28px 40px}} .view.active{{display:block}} .trace{{border-left:2px solid #334155;padding-left:16px}}
.item{{background:#182235;border:1px solid #334155;border-left:4px solid #64748b;border-radius:7px;margin:8px 0;overflow:hidden}}
.item.user{{border-left-color:#3b82f6}} .item.thinking{{border-left-color:#8b5cf6}} .item.text{{border-left-color:#22c55e}} .item.tool_use{{border-left-color:#f43f5e}} .item.post_tool_use{{border-left-color:#14b8a6}} .item.error{{border-left-color:#ef4444;background:#2b1820}}
.item-head{{display:grid;grid-template-columns:90px 115px 130px 1fr;gap:10px;padding:9px 12px;cursor:pointer;align-items:center}} .time{{color:#94a3b8;font-family:monospace;font-size:12px}}
.kind{{font-weight:700}} .agent{{color:#93c5fd;font-family:monospace;overflow:hidden;text-overflow:ellipsis}} .summary{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.detail{{display:none;white-space:pre-wrap;padding:12px;background:#0f172a;border-top:1px solid #334155;max-height:620px;overflow:auto;font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px}} .item.open .detail{{display:block}}
.group{{background:#16213e;border:1px solid #334155;border-radius:8px;margin:10px 0}} .group-head{{padding:11px 14px;cursor:pointer;font-weight:700}} .group-body{{display:none;padding:0 12px 10px}} .group.open .group-body{{display:block}} .empty{{padding:25px;color:#94a3b8;font-style:italic}}
@media(max-width:800px){{.header{{position:static}}.filters{{position:static}}.item-head{{grid-template-columns:70px 90px 1fr}}.agent{{display:none}}}}
</style></head><body>
<div class="header"><div class="top"><div><h1>Codex Session Trace</h1>
<div class="meta">{project}<br>{html.escape(session['id'])}<br>{model_mix} · {html.escape(session['duration'])}</div></div>
<div><select id="sessionSelect"></select><div class="meta" id="sessionCmd"></div></div></div>
<div class="stats">
<div class="stat"><b>{format_tokens(usage['total_tokens'])}</b><span>Total tokens</span></div>
<div class="stat"><b>{format_tokens(usage['input_tokens'])}</b><span>Input</span></div>
<div class="stat"><b>{format_tokens(usage['cached_input_tokens'])}</b><span>Cached input</span></div>
<div class="stat"><b>{format_tokens(usage['output_tokens'])}</b><span>Output</span></div>
<div class="stat"><b>{cost}</b><span>Est. cost</span></div>
<div class="stat"><b>{session.get('agent_count', 1)}</b><span>Agents</span></div>
<div class="stat"><b>{sum(session['tool_counts'].values())}</b><span>Tool calls</span></div>
</div>{unknown_note}</div>
<div class="filters">
<select id="eventFilter"><option value="">All events</option><option value="user">User</option><option value="thinking">Thinking</option><option value="tool_use">Tool use</option><option value="post_tool_use">Tool output</option><option value="text">Response</option><option value="lifecycle">Lifecycle</option></select>
<select id="agentFilter"><option value="">All agents</option>{agent_options}</select>
<input id="search" type="text" placeholder="Search trace">
<label><input id="raw" type="checkbox"> Raw JSON</label>
<button class="tab" id="expand">Expand all</button><button class="tab" id="collapse">Collapse all</button>
</div>
<div class="tabs"><button class="tab active" data-view="timeline">Timeline</button> <button class="tab" data-view="tree">Agent tree</button></div>
<div id="timeline" class="view active"><div class="trace"></div></div><div id="tree" class="view"><div class="tree"></div></div>
<script>
const events={events_json}; const allSessions={sessions_json}; const currentId={json.dumps(session['id'])};
const eventFilter=document.getElementById('eventFilter'),agentFilter=document.getElementById('agentFilter'),search=document.getElementById('search'),raw=document.getElementById('raw');
const sessionSelect=document.getElementById('sessionSelect'),sessionCmd=document.getElementById('sessionCmd');
allSessions.forEach(s=>{{const o=document.createElement('option');o.value=s.id;o.textContent=s.id.slice(0,8)+' · '+(s.duration||'?')+' · '+(s.summary||'(no summary)').slice(0,42);o.selected=s.id===currentId;sessionSelect.appendChild(o)}});
sessionSelect.onchange=()=>{{sessionCmd.textContent=sessionSelect.value===currentId?'':'Run: codex-session-analyzer '+sessionSelect.value.slice(0,8)+' --open'}};
function show(e){{if(eventFilter.value&&e.event_type!==eventFilter.value)return false;if(agentFilter.value&&e.agent_id!==agentFilter.value)return false;const q=search.value.trim().toLowerCase();return !q||[e.summary,e.details,e.raw,e.kind,e.name,e.agent_label].join(' ').toLowerCase().includes(q)}}
function time(ts){{if(!ts)return '';return new Date(ts).toLocaleTimeString()}}
function card(e){{const d=document.createElement('div');d.className='item '+(e.event_type||'lifecycle')+(e.error?' error':'');const h=document.createElement('div');h.className='item-head';
for(const [cls,text] of [['time',time(e.ts)],['kind',e.event_type||e.kind],['agent',e.agent_label||'main'],['summary',[e.name,e.summary].filter(Boolean).join(' · ')]]){{const x=document.createElement('div');x.className=cls;x.textContent=text;h.appendChild(x)}}
const detail=document.createElement('div');detail.className='detail';detail.textContent=(e.details||'(no details)')+(raw.checked&&e.raw?'\\n\\n--- Raw JSON ---\\n'+e.raw:'');h.onclick=()=>d.classList.toggle('open');d.append(h,detail);return d}}
function renderTimeline(){{const root=document.querySelector('.trace');root.innerHTML='';let n=0;events.forEach(e=>{{if(show(e)){{root.appendChild(card(e));n++}}}});if(!n)root.innerHTML='<div class="empty">No matching events.</div>'}}
function renderTree(){{const root=document.querySelector('.tree');root.innerHTML='';const groups={{}};events.forEach(e=>{{if(show(e))(groups[e.agent_label||'main']??=[]).push(e)}});Object.entries(groups).forEach(([name,items])=>{{const g=document.createElement('div');g.className='group';const h=document.createElement('div');h.className='group-head';h.textContent='▶ '+name+' · '+items.length+' events';const b=document.createElement('div');b.className='group-body';items.forEach(e=>b.appendChild(card(e)));h.onclick=()=>g.classList.toggle('open');g.append(h,b);root.appendChild(g)}});if(!Object.keys(groups).length)root.innerHTML='<div class="empty">No matching events.</div>'}}
function render(){{renderTimeline();renderTree()}} [eventFilter,agentFilter,raw].forEach(x=>x.onchange=render);search.oninput=render;
document.querySelectorAll('[data-view]').forEach(t=>t.onclick=()=>{{document.querySelectorAll('[data-view]').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.view').forEach(x=>x.classList.remove('active'));t.classList.add('active');document.getElementById(t.dataset.view).classList.add('active')}});
document.getElementById('expand').onclick=()=>document.querySelectorAll('.item,.group').forEach(x=>x.classList.add('open'));document.getElementById('collapse').onclick=()=>document.querySelectorAll('.item,.group').forEach(x=>x.classList.remove('open'));render();
</script></body></html>"""


def load_sessions(sessions_dir, archived_dir, max_chars=0, include_raw=False):
    sessions = []
    for path in iter_session_files(sessions_dir, archived_dir):
        try:
            sessions.append(parse_session(path, max_chars=max_chars, include_raw=include_raw))
        except OSError as error:
            print(f"warning: cannot read {path}: {error}", file=sys.stderr)
    return sessions


def write_or_print(output, args, is_html=False):
    if args.out_file:
        Path(args.out_file).expanduser().write_text(output, encoding="utf-8")
        print(f"Written to {args.out_file}", file=sys.stderr)
        if args.open and is_html:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", args.out_file], check=False)
        return 0
    if args.open and is_html:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as handle:
            handle.write(output)
            path = handle.name
        print(f"Opening {path}", file=sys.stderr)
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", path], check=False)
        return 0
    print(output)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Analyze Codex session logs, history, agents, tokens, and cost",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("session_id", nargs="?", help="Session id/prefix or rollout JSONL path")
    parser.add_argument("--latest", action="store_true", help="Use the newest root session (global unless --project is set)")
    parser.add_argument("--list", action="store_true", help="List root sessions with summaries")
    parser.add_argument("--all", action="store_true", help="Include standalone sub-agent rollouts in --list")
    parser.add_argument("--project", help="Only consider sessions whose cwd exactly matches this path")
    parser.add_argument("--output", choices=("json", "html"), default="html", help="Detailed output format")
    parser.add_argument("--digest", action="store_true", help="Print a markdown continuation digest")
    parser.add_argument("--overview", action="store_true", help="Show main/sub-agent topology and token totals")
    parser.add_argument("--agents", action="store_true", help="Alias for --overview")
    parser.add_argument("--path", action="store_true", help="Print only the resolved root transcript path")
    parser.add_argument("--open", action="store_true", help="Open HTML viewer in the browser")
    parser.add_argument("--out-file", help="Write detailed output to this path")
    parser.add_argument("--limit", type=int, default=20, help="Limit --list output (0 = unlimited)")
    parser.add_argument("--codex-home", default=str(DEFAULT_CODEX_HOME), help="Codex state directory")
    parser.add_argument("--sessions-dir", help="Override active rollout directory")
    parser.add_argument("--archived-dir", help="Override archived rollout directory (empty string disables)")
    parser.add_argument("--max-chars", type=int, default=0, help="Truncate event details (0 = unlimited)")
    parser.add_argument("--raw", action="store_true", help="Include raw JSON records in detailed output")
    parser.add_argument("--no-raw", action="store_true", help="Do not include raw JSON (default)")
    args = parser.parse_args()

    codex_home = Path(args.codex_home).expanduser()
    sessions_dir = Path(args.sessions_dir).expanduser() if args.sessions_dir else codex_home / "sessions"
    if args.archived_dir == "":
        archived_dir = None
    else:
        archived_dir = Path(args.archived_dir).expanduser() if args.archived_dir else codex_home / "archived_sessions"
    if not sessions_dir.is_dir() and not (archived_dir and archived_dir.is_dir()):
        print(f"No Codex session directories found under {codex_home}", file=sys.stderr)
        return 1

    sessions = load_sessions(
        sessions_dir,
        archived_dir,
        max_chars=args.max_chars,
        include_raw=args.raw and not args.no_raw,
    )
    if not sessions:
        print("No Codex sessions found", file=sys.stderr)
        return 1

    if args.list:
        selected = [session for session in sessions if args.all or not session["is_subagent"]]
        selected = [session for session in selected if same_project(session, args.project)]
        # Roll up descendants so list columns reflect the full task.
        id_map = {session["id"]: session for session in sessions}
        rows = [combine_thread(session, sessions) if session["id"] in id_map else session for session in selected]
        if args.output == "json":
            ordered = sorted(rows, key=session_sort_key, reverse=True)[: args.limit or None]
            print(json.dumps(serializable([session_list_record(row) for row in ordered]), indent=2))
        else:
            print_list(rows, limit=args.limit)
        return 0

    session = None
    matches = []
    if args.session_id and Path(args.session_id).expanduser().is_file():
        path = str(Path(args.session_id).expanduser().resolve())
        session = next((item for item in sessions if str(Path(item["path"]).resolve()) == path), None)
        if not session:
            session = parse_session(path, args.max_chars, args.raw and not args.no_raw)
            sessions.append(session)
    elif args.session_id:
        session, matches = select_session(sessions, args.session_id)
    else:
        candidates = [item for item in sessions if not item["is_subagent"]]
        if args.project:
            candidates = [item for item in candidates if same_project(item, args.project)]
        elif not args.latest:
            candidates = [item for item in candidates if same_project(item, os.getcwd())]
        if candidates:
            session = max(candidates, key=session_sort_key)

    if not session:
        if args.session_id:
            print(f"No unique match for '{args.session_id}'. Matches: {len(matches)}", file=sys.stderr)
            for match in matches[:10]:
                print(f"- {match['id']} ({match['path']})", file=sys.stderr)
        else:
            scope = args.project or ("all projects" if args.latest else os.getcwd())
            print(f"No root Codex session found for {scope}", file=sys.stderr)
        return 1

    # A selected child is useful on its own; a selected root includes descendants.
    combined = combine_thread(session, sessions)
    if args.path:
        print(session["path"])
        return 0
    if args.overview or args.agents:
        overview = build_overview(session, sessions)
        output = json.dumps(serializable(overview), indent=2) if args.output == "json" else format_overview(overview)
        return write_or_print(output, args)
    if args.digest:
        return write_or_print(generate_digest(combined), args)
    if args.output == "json":
        return write_or_print(json.dumps(serializable(combined), indent=2), args)

    root_sessions = sorted(
        (item for item in sessions if not item["is_subagent"]),
        key=session_sort_key,
        reverse=True,
    )
    return write_or_print(build_html(combined, root_sessions), args, is_html=True)


if __name__ == "__main__":
    raise SystemExit(main())
