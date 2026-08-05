#!/usr/bin/env python3
"""
Codex Session Narrative
=======================

Reconstructs a Codex session as a human-readable story: prompt -> reasoning ->
tool calls -> what each call returned -> reply, turn by turn.

Where the data comes from (all of it already sits in the rollout JSONL; none of
it needs extra logging):

  turn_id             on task_started, turn_context, reasoning, message, every
                      function/custom tool call and its output, and
                      patch_apply_end -- 100% coverage in practice. Turn
                      boundaries are therefore EXACT here, not reconstructed.
                      (Claude's transcript has no such field; its narrative has
                      to walk parentUuid chains to find the owning prompt.)
  token_count         info.total_token_usage is cumulative, so per-turn spend is
                      the delta between consecutive snapshots. Cached input and
                      reasoning output are SUBSETS of input/output, never added
                      on top -- see split_usage().
  custom_tool_call    `exec`, whose input is a JavaScript program calling
                      tools.shell_command({command, workdir}). Decoded by
                      parser.decode_exec_script rather than regex-scraped.
  patch_apply_end     the real record of a file edit: per-file add/update plus
                      success. This is Codex's structuredPatch equivalent.
  mcp_tool_call_end   server, tool, arguments, duration, and Ok/Err.
  turn_context        per-turn model, cwd, approval policy, sandbox.
  thread_settings_    model / reasoning_effort / personality changes. Model
    applied           switches are RECORDED here, not inferred.
  compacted           context compaction, with the replacement history length.
  turn_aborted,       interruptions and rollbacks.
    thread_rolled_back
  sub_agent_activity  agent_thread_id + agent_path ('/root/independent_audit'),
                      so sub-agent spawns appear inline in the turn that caused
                      them.

Retries are NOT recorded anywhere; they are inferred (see infer_retries).

Usage:
    narrative.py                       # latest session in this cwd, opens HTML
    narrative.py <session-id>          # a specific session (id or prefix)
    narrative.py <path/to.jsonl>       # a rollout by path
    narrative.py --text                # terminal rendering instead of HTML
    narrative.py --out-file report.html
"""
import argparse
import collections
import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parser import (  # noqa: E402
    DEFAULT_CODEX_HOME,
    agent_identity,
    agent_name_from_path,
    decode_exec_script,
    exec_commands,
    extract_text,
    extract_reasoning_text,
    get_pricing,
    is_instruction_text,
    iter_session_files,
    mcp_call_failed,
    normalize_usage,
    parse_ts,
    patch_files,
    pricing_mod,
    read_jsonl,
    summarize_text,
)

TOK_PER_CHAR = 0.25          # result bytes -> approx tokens
HEAVY_FLOOR = 1200           # a "heavy" result is at least this many tokens
TOKEN_KEYS = ("in", "cache_read", "out")


# ===========================================================================
# Formatting helpers
# ===========================================================================
def dur(s):
    """Sub-second precision matters: most tool calls finish in well under 1s."""
    if s is None:
        return "-"
    s = max(0.0, float(s))
    if s < 1:
        return f"{s:.2f}s"
    if s < 10:
        return f"{s:.1f}s"
    if s < 60:
        return f"{s:.0f}s"
    m, r = divmod(int(s), 60)
    if m < 60:
        return f"{m}m {r:02d}s" if r else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def tok(n):
    if n is None:
        return "-"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def clip(text, n):
    """Trim to a sentence boundary when one is near the limit, else a word."""
    t = " ".join(str(text or "").split())
    if len(t) <= n:
        return t
    cut = t[:n]
    stop = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if stop > n * 0.6:
        return cut[:stop + 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n * 0.5 else cut).rstrip(" ,;:-") + "…"


def zero_tokens():
    return {k: 0 for k in TOKEN_KEYS}


def add_tokens(a, b):
    for k in TOKEN_KEYS:
        a[k] += b[k]
    return a


def split_usage(usage):
    """Codex usage -> billable buckets.

    Codex reports SUBSETS, not addends: cached_input_tokens is already inside
    input_tokens, and reasoning_output_tokens is already inside output_tokens.
    Adding either back on top double-counts, which is the single easiest way to
    get a Codex token report badly wrong.
    """
    u = normalize_usage(usage)
    cached = min(u["cached_input_tokens"], u["input_tokens"])
    return {
        "in": max(u["input_tokens"] - cached, 0),
        "cache_read": cached,
        "out": u["output_tokens"],
    }


def cost_of(t, model):
    """USD for one turn at that turn's model, or None when unpriced."""
    return get_pricing().cost(model, {
        "input": t["in"], "cache_read": t["cache_read"],
        "cache_write": 0, "output": t["out"],
    })


def secs(a, b):
    if not a or not b:
        return None
    return max(0.0, (b - a).total_seconds())


# ===========================================================================
# Tool result renderers  (Codex-shaped, one per tool family)
# ===========================================================================
EXIT_RE = re.compile(r"Exit code:\s*(-?\d+)", re.I)
PATCH_FILE_RE = re.compile(r"\*\*\* (Add|Update|Delete) File:\s*([^\\\n\"]+)")


def shell_verdict(text):
    """Did this exec call succeed? -> (ok, human verdict)

    Codex reports two independent things, and only using one of them gives the
    wrong answer:

      "Script completed" / "Script failed"  the JS wrapper's own outcome
      "Exit code: N"                        the shell command inside it

    A script can complete cleanly while the command it ran exits 1, so the
    command's exit code wins when present. "Script running with cell ID N" is an
    async call that had not finished -- unknown, not failed.
    """
    if not text:
        return None, "no output recorded"
    if text.startswith("Script failed"):
        return False, "script failed"
    match = EXIT_RE.search(text)
    if match:
        code = int(match.group(1))
        return code == 0, f"exit {code}"
    if text.startswith("Script completed"):
        return True, "ok"
    if text.startswith("Script running"):
        return None, "still running"
    return None, "completed"


def _t(s, n=120):
    return clip(s, n)


def output_text(payload):
    """The text a tool output record carried, whatever shape it used."""
    out = payload.get("output")
    if out is None:
        out = payload.get("result")
    if isinstance(out, str):
        return out
    text = extract_text(out)
    if text:
        return text
    return json.dumps(out, ensure_ascii=False) if out is not None else ""


def r_shell(call, out_payload):
    """`exec` -- a JS script that shells out one or more times."""
    script = call.get("input") or ""
    commands = exec_commands(script)
    calls = decode_exec_script(script)
    workdir = next((c["args"].get("workdir") for c in calls
                    if c["args"].get("workdir")), "")

    text = output_text(out_payload) if out_payload else ""
    ok, verdict = shell_verdict(text)

    # Codex applies patches two ways: a patch_apply_end event, and an exec
    # script carrying an inline "*** Begin Patch" envelope. Without this the
    # second kind renders as a wall of escaped JavaScript, which is exactly the
    # call you most want to read when it failed.
    patch_targets = PATCH_FILE_RE.findall(script)
    if patch_targets:
        verbs = {verb.lower() for verb, _ in patch_targets}
        names = ", ".join(Path(p).name for _, p in patch_targets[:3])
        if len(patch_targets) > 3:
            names += f" +{len(patch_targets) - 3}"
        return {
            "tool": "apply_patch",
            "call": names,
            "detail": f"{'/'.join(sorted(verbs))} · {len(patch_targets)} file(s)",
            "ok": ok,
            "result": verdict,
            "preview": "\n".join(f"{v} {p}" for v, p in patch_targets),
            "bytes": len(text),
        }

    label = commands[0] if commands else _t(script, 100)
    if len(commands) > 1:
        label += f"   (+{len(commands) - 1} more)"

    # Everything before the last "Output:" marker is harness preamble
    # ("Script completed / Wall time / Exit code"), not the command's output.
    body = text
    marker = text.rfind("Output:")
    if marker != -1:
        body = text[marker + len("Output:"):]

    return {
        "tool": "shell",
        "call": label,
        "detail": f"in {workdir}" if workdir else "",
        "ok": ok,
        "result": verdict
                  + (f" · {len(body.strip().splitlines())} lines" if body.strip() else " · no output"),
        "preview": body.strip()[:3000],
        "bytes": len(text),
    }


def r_patch(payload):
    """patch_apply_end -- the authoritative record of a file edit."""
    files = patch_files(payload)
    changes = payload.get("changes") or {}
    adds = sum(1 for c in changes.values()
               if isinstance(c, dict) and c.get("type") == "add")
    updates = len(files) - adds
    bits = []
    if adds:
        bits.append(f"{adds} added")
    if updates:
        bits.append(f"{updates} updated")
    names = ", ".join(Path(f).name for f in files[:3])
    if len(files) > 3:
        names += f" +{len(files) - 3}"
    return {
        "tool": "apply_patch",
        "call": names or "(no files)",
        "detail": " · ".join(bits),
        "ok": payload.get("success") is not False,
        "result": (payload.get("stderr") or "").strip()[:200]
                  or f"{len(files)} file(s)",
        "preview": "\n".join(files),
        "bytes": len(payload.get("stdout") or ""),
    }


def r_mcp(payload):
    """mcp_tool_call_end -- an external app call."""
    inv = payload.get("invocation") or {}
    label = f"{inv.get('server') or '?'}.{inv.get('tool') or '?'}"
    duration = payload.get("duration") or {}
    took = duration.get("secs") if isinstance(duration, dict) else None
    result = payload.get("result")
    body = json.dumps(result, ensure_ascii=False) if result is not None else ""
    failed = mcp_call_failed(payload)
    return {
        "tool": "mcp",
        "call": label,
        "detail": _t(json.dumps(inv.get("arguments") or {}, ensure_ascii=False), 100),
        "ok": not failed,
        "result": ("failed" if failed else "ok")
                  + (f" · {took}s" if took is not None else ""),
        "preview": body[:3000],
        "bytes": len(body),
    }


def r_web_search(payload):
    query = payload.get("query") or payload.get("q") or ""
    return {
        "tool": "web_search",
        "call": _t(query, 100) or "(query not recorded)",
        "detail": "",
        "ok": True,
        "result": "completed",
        "preview": json.dumps(payload, ensure_ascii=False)[:2000],
        "bytes": 0,
    }


def r_generic(name, call, out_payload):
    args = call.get("input") or call.get("arguments") or ""
    text = output_text(out_payload) if out_payload else ""
    ok, verdict = shell_verdict(text)
    return {
        "tool": name or "tool",
        "call": _t(args if isinstance(args, str) else json.dumps(args), 110),
        "detail": "",
        "ok": ok,
        "result": _t(text, 90) or verdict,
        "preview": text[:3000],
        "bytes": len(text),
    }


# ===========================================================================
# Retry inference (heuristic — nothing in the rollout marks a retry)
# ===========================================================================
def infer_retries(tools):
    """A failed call followed by the same tool on a near-identical target.

    apply_patch and mcp compare exactly (a path or a server.tool is an exact
    match or nothing); shell commands fall back to fuzzy matching, since a retry
    usually tweaks a flag rather than repeating verbatim.
    """
    from difflib import SequenceMatcher

    exact = {"apply_patch", "mcp"}
    for i, s in enumerate(tools):
        if s.get("ok") is not False:
            continue
        for j in range(i + 1, min(i + 5, len(tools))):
            nxt = tools[j]
            if nxt["tool"] != s["tool"] or "retry_of" in nxt:
                continue
            if s["tool"] in exact:
                if str(s.get("call")) != str(nxt.get("call")):
                    continue
                sim = 1.0
            else:
                sim = SequenceMatcher(None, str(s.get("call")),
                                      str(nxt.get("call"))).ratio()
                if sim <= 0.55:
                    continue
            nxt["retry_of"] = i
            nxt["retry_similarity"] = round(sim, 2)
            s["retried_by"] = j
            break


# ===========================================================================
# Build
# ===========================================================================
def turn_id_of(record):
    """Codex stamps turn_id in two places depending on the record family."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    direct = payload.get("turn_id")
    if direct:
        return direct
    meta = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(meta, dict):
        return meta.get("turn_id")
    return None


def build(path):
    records = read_jsonl(path)
    meta = {}
    for record in records:
        if record.get("type") == "session_meta":
            meta = record.get("payload") or {}
            break
    identity = agent_identity(meta)

    # --- pass 1: group records into turns -----------------------------------
    # turn_id is present on every record family that matters. The handful that
    # carry none (token_count, world_state, compacted) are attributed to the
    # turn that was open when they were written, which is what "during this
    # turn" means for them anyway.
    order = []
    buckets = collections.OrderedDict()
    current = None
    for record in records:
        tid = turn_id_of(record)
        if tid:
            current = tid
        key = current or "__preamble__"
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(record)

    prev_total = normalize_usage({})
    model = None
    effort = None
    turns = []

    for tid in order:
        group = buckets[tid]
        tokens = zero_tokens()
        steps = []
        prompt = ""
        pending = {}
        # Codex mirrors each reasoning block into BOTH response_item.reasoning
        # and event_msg.agent_reasoning, so taking them at face value prints
        # every thought twice. Keep the first of each identical text per turn.
        seen_reasoning = set()
        turn_model = None
        requests = 0
        spawned = []
        markers = []

        for record in group:
            ts = parse_ts(record.get("timestamp"))
            outer = record.get("type")
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            ptype = payload.get("type")

            if outer == "turn_context":
                model = payload.get("model") or model
                turn_model = turn_model or payload.get("model")
                continue

            if outer == "compacted":
                markers.append({"kind": "compacted", "ts": ts,
                                "text": "context compacted"})
                continue

            if outer == "event_msg":
                if ptype == "user_message":
                    text = payload.get("message") or ""
                    if text and not is_instruction_text(text) and not prompt:
                        prompt = text
                elif ptype == "agent_message":
                    text = (payload.get("message") or "").strip()
                    if text:
                        steps.append({"t": "say", "text": text, "ts": ts})
                elif ptype == "agent_reasoning":
                    text = (payload.get("text") or "").strip()
                    key = " ".join(text.split())
                    if text and key not in seen_reasoning:
                        seen_reasoning.add(key)
                        steps.append({"t": "think", "text": text, "ts": ts})
                elif ptype == "token_count":
                    info = payload.get("info") or {}
                    total = normalize_usage(info.get("total_token_usage"))
                    if any(total.values()):
                        delta = {k: max(total[k] - prev_total[k], 0) for k in total}
                        if not any(delta.values()):
                            delta = normalize_usage(info.get("last_token_usage"))
                        if any(delta.values()):
                            requests += 1
                            add_tokens(tokens, split_usage(delta))
                        prev_total = total
                elif ptype == "thread_settings_applied":
                    settings = payload.get("thread_settings") or {}
                    new_model = settings.get("model")
                    new_effort = settings.get("reasoning_effort")
                    changed = []
                    if new_model and new_model != model:
                        changed.append(f"model {model or '?'} → {new_model}")
                    if new_effort and new_effort != effort:
                        changed.append(f"effort {effort or '?'} → {new_effort}")
                    model = new_model or model
                    turn_model = turn_model or new_model
                    effort = new_effort or effort
                    if changed:
                        markers.append({"kind": "settings", "ts": ts,
                                        "text": "; ".join(changed)})
                elif ptype == "sub_agent_activity":
                    name = agent_name_from_path(payload.get("agent_path"))
                    kind = payload.get("kind") or ""
                    spawned.append({"name": name, "kind": kind,
                                    "thread": payload.get("agent_thread_id") or ""})
                    markers.append({"kind": "subagent", "ts": ts,
                                    "text": f"sub-agent {name or '?'} {kind}".strip()})
                elif ptype == "context_compacted":
                    markers.append({"kind": "compacted", "ts": ts,
                                    "text": "context compacted"})
                elif ptype == "turn_aborted":
                    markers.append({"kind": "aborted", "ts": ts,
                                    "text": "turn aborted"})
                elif ptype == "thread_rolled_back":
                    markers.append({"kind": "aborted", "ts": ts,
                                    "text": "thread rolled back"})
                elif ptype == "patch_apply_end":
                    d = r_patch(payload)
                    d.update({"t": "tool", "ts": ts, "latency": None})
                    steps.append(d)
                elif ptype == "mcp_tool_call_end":
                    d = r_mcp(payload)
                    duration = payload.get("duration") or {}
                    d.update({"t": "tool", "ts": ts,
                              "latency": duration.get("secs")
                              if isinstance(duration, dict) else None})
                    steps.append(d)
                elif ptype == "web_search_end":
                    d = r_web_search(payload)
                    d.update({"t": "tool", "ts": ts, "latency": None})
                    steps.append(d)
                continue

            if outer == "response_item":
                if ptype == "reasoning":
                    text = (extract_reasoning_text(payload) or "").strip()
                    key = " ".join(text.split())
                    if text and key not in seen_reasoning:
                        seen_reasoning.add(key)
                        steps.append({"t": "think", "text": text, "ts": ts})
                elif ptype in ("custom_tool_call", "function_call"):
                    call_id = payload.get("call_id") or payload.get("id")
                    pending[call_id] = {
                        "name": payload.get("name") or "tool",
                        "input": payload.get("input") or payload.get("arguments") or "",
                        "ts": ts,
                    }
                elif ptype in ("custom_tool_call_output", "function_call_output"):
                    call_id = payload.get("call_id")
                    call = pending.pop(call_id, None)
                    if call is None:
                        continue
                    name = call["name"]
                    if name in ("exec", "shell", "shell_command", "local_shell"):
                        d = r_shell(call, payload)
                    else:
                        d = r_generic(name, call, payload)
                    d.update({
                        "t": "tool", "ts": call["ts"],
                        "latency": secs(call["ts"], ts),
                        "result_bytes": d.pop("bytes", 0),
                    })
                    steps.append(d)
                continue

        # Calls with no output: interrupted, or still running when the rollout
        # ended. Surfacing them beats dropping them -- a missing result is
        # exactly the kind of thing you open a narrative to find.
        for call_id, call in pending.items():
            name = call["name"]
            d = (r_shell(call, None) if name in ("exec", "shell", "shell_command")
                 else r_generic(name, call, None))
            d.update({"t": "tool", "ts": call["ts"], "latency": None,
                      "ok": None, "result_bytes": 0,
                      "finding": "no result recorded (interrupted or still running)"})
            steps.append(d)

        for s in steps:
            s.setdefault("result_bytes", 0)
            s["result_tokens"] = int(s["result_bytes"] * TOK_PER_CHAR)

        # Markers (compaction, aborts, sub-agent spawns, settings changes) are
        # part of the story, so they belong in the timeline where they happened
        # rather than in a block at the top of the turn.
        for m in markers:
            steps.append({"t": "mark", "kind": m["kind"], "text": m["text"],
                          "ts": m["ts"], "result_bytes": 0, "result_tokens": 0})
        steps.sort(key=lambda s: (s["ts"] is None, s["ts"] or 0))
        tools = [s for s in steps if s["t"] == "tool"]
        infer_retries(tools)

        stamps = [s["ts"] for s in steps if s.get("ts")]
        stamps += [m["ts"] for m in markers if m.get("ts")]
        group_ts = [parse_ts(r.get("timestamp")) for r in group]
        group_ts = [t for t in group_ts if t]
        t_start = min(stamps) if stamps else (group_ts[0] if group_ts else None)
        t_end = max(stamps) if stamps else (group_ts[-1] if group_ts else None)

        if not steps and not prompt and not markers:
            continue

        turns.append({
            "n": len(turns) + 1,
            "turn_id": tid,
            "prompt": prompt,
            "model": turn_model or model,
            "effort": effort,
            "start": t_start, "end": t_end,
            "duration": secs(t_start, t_end),
            "steps": steps, "markers": markers, "tokens": tokens,
            "api_requests": requests,
            "spawned": spawned,
            "n_tools": len(tools),
            "n_failed": sum(1 for s in tools if s.get("ok") is False),
            "n_retries": sum(1 for s in tools if "retry_of" in s),
        })

    session_tokens = zero_tokens()
    session_cost = 0.0
    unpriced = set()
    for t in turns:
        add_tokens(session_tokens, t["tokens"])
        c = cost_of(t["tokens"], t["model"])
        t["cost"] = c
        if c is None:
            if any(t["tokens"][k] for k in TOKEN_KEYS):
                unpriced.add(t["model"] or "unknown")
        else:
            session_cost += c

    all_ts = [parse_ts(r.get("timestamp")) for r in records]
    all_ts = [t for t in all_ts if t]
    first_prompt = next((t["prompt"] for t in turns if t["prompt"]), "")

    return {
        "session_id": meta.get("id") or Path(path).stem.replace("rollout-", ""),
        "title": clip(first_prompt, 70) or "Untitled session",
        "model": next((t["model"] for t in reversed(turns) if t["model"]), model or ""),
        "effort": effort,
        "agent_label": identity["agent_label"],
        "is_subagent": identity["is_subagent"],
        "project": meta.get("cwd") or "",
        "cli_version": meta.get("cli_version") or "",
        "file": str(path),
        "start": min(all_ts) if all_ts else None,
        "end": max(all_ts) if all_ts else None,
        "turns": turns,
        "tokens": session_tokens,
        "cost": session_cost,
        "unpriced_models": sorted(unpriced),
    }


def calibrate_heavy(d):
    """'Heavy' is judged against this session's own p90, with an absolute floor."""
    v = sorted((s.get("result_tokens") or 0)
               for t in d["turns"] for s in tools_of(t))
    return max(HEAVY_FLOOR, v[int(len(v) * 0.9)]) if v else HEAVY_FLOOR


def tools_of(turn):
    return [s for s in turn["steps"] if s["t"] == "tool"]


# ===========================================================================
# Terminal surface
# ===========================================================================
SPARK = "▁▂▃▄▅▆▇█"


def spark(vals, width=48):
    """One cell per call, downsampled to `width` so a 400-call turn still fits.

    Buckets take the max, not the mean: the point of the ribbon is to show where
    the expensive results landed, and averaging hides a single huge one.
    """
    if not vals:
        return ""
    if len(vals) > width:
        size = len(vals) / width
        vals = [max(vals[int(i * size):max(int((i + 1) * size), int(i * size) + 1)])
                for i in range(width)]
    hi = max(vals) or 1
    return "".join(SPARK[min(int(v / hi * (len(SPARK) - 1)), len(SPARK) - 1)]
                   for v in vals)


def wrap(text, width, indent):
    out = []
    for para in str(text or "").split("\n"):
        line = ""
        for word in para.split():
            if len(line) + len(word) + 1 > width:
                out.append(indent + line)
                line = word
            else:
                line = f"{line} {word}".strip()
        out.append(indent + line)
    return out


def token_line(t):
    """What the API actually had to process this turn.

    Codex re-sends the whole conversation every turn, so cache_read dominates
    and is the number that explains a session's token bill; 'new' is the part
    that was actually novel this turn.
    """
    return (f"{tok(t['in'])} new · {tok(t['cache_read'])} from cache · "
            f"{tok(t['out'])} out")


def render_text(d, heavy):
    L, W = [], 92
    all_tools = [s for t in d["turns"] for s in tools_of(t)]
    nf = sum(t["n_failed"] for t in d["turns"])
    nr = sum(t["n_retries"] for t in d["turns"])
    tk = d["tokens"]

    L.append("━" * W)
    L.append(f"  {d['title']}")
    ident = f"  {d['session_id'][:8]} · {d['model']}"
    if d.get("effort"):
        ident += f" ({d['effort']})"
    if d["project"]:
        ident += f" · {Path(d['project']).name}"
    if d["is_subagent"]:
        ident += f" · sub-agent {d['agent_label']}"
    L.append(ident)
    L.append(f"  {len(d['turns'])} turns · {len(all_tools)} tool calls · "
             f"{nf} failed · {nr} retried")
    cost = f"~${d['cost']:.2f}" if not d["unpriced_models"] else "cost n/a"
    L.append(f"  {token_line(tk)} · {cost}")
    L.append("━" * W)

    for t in d["turns"]:
        tools = tools_of(t)
        bars = spark([s.get("result_tokens") or 0 for s in tools])
        L.append("")
        head = f"TURN {t['n']:02d}  ·  {dur(t['duration'])}  ·  {len(tools)} calls"
        L.append(f"{head}{bars.rjust(max(0, W - len(head)))}")
        L.append(f"           {token_line(t['tokens'])}")
        if t["prompt"]:
            L.append("│")
            L.append("├─ YOU")
            L.extend(wrap(clip(t["prompt"], 400), W - 6, "│  "))

        for s in t["steps"]:
            L.append("│")
            if s["t"] == "mark":
                icon = {"aborted": "⚠", "compacted": "⟳", "subagent": "⑂",
                        "settings": "⚙"}.get(s["kind"], "·")
                L.append(f"│  {icon} {s['text']}")
            elif s["t"] == "think":
                L.append("├─ THINKING")
                L.extend(wrap(clip(s["text"], 300), W - 6, "│  "))
            elif s["t"] == "say":
                L.append("├─ CODEX")
                L.extend(wrap(clip(s["text"], 500), W - 6, "│  "))
            else:
                mark = {True: "✓", False: "✗", None: "?"}[s.get("ok")]
                flags = []
                if "retry_of" in s:
                    flags.append(f"retry of #{s['retry_of'] + 1}")
                if (s.get("result_tokens") or 0) >= heavy:
                    flags.append(f"heavy ~{tok(s['result_tokens'])} tok")
                if s.get("finding"):
                    flags.append(s["finding"])
                suffix = f"   [{'; '.join(flags)}]" if flags else ""
                L.append(f"├─ {mark} {s['tool']}  {dur(s.get('latency'))}{suffix}")
                L.extend(wrap(clip(s["call"], 200), W - 6, "│    "))
                if s.get("detail"):
                    L.extend(wrap(s["detail"], W - 6, "│    "))
                L.append(f"│    → {clip(s.get('result'), 150)}")

    L.append("")
    L.append("━" * W)
    L.append("Turn boundaries are exact (turn_id). Cached input and reasoning "
             "output are subsets,")
    L.append("not extras. Retries are inferred, not recorded. Cost is an "
             "estimate; provider billing")
    L.append("is authoritative.")
    if d["unpriced_models"]:
        L.append(f"No price configured for {', '.join(d['unpriced_models'])} — "
                 f"add rates to {pricing_mod.USER_FILE}.")
    return "\n".join(L)


# ===========================================================================
# HTML surface
# ===========================================================================
def e(s):
    return html.escape(str(s or ""))


def prose(text, limit=1400):
    body = e(clip(text, limit))
    return f'<div class="prose">{body}</div>'


def ribbon(steps, heavy):
    cells = []
    for s in steps:
        if s["t"] != "tool":
            continue
        state = {True: "ok", False: "bad", None: "unk"}[s.get("ok")]
        if (s.get("result_tokens") or 0) >= heavy:
            state += " hv"
        cells.append(f'<i class="{state}" title="{e(clip(s["call"], 90))}"></i>')
    return f'<div class="ribbon">{"".join(cells)}</div>' if cells else ""


def step_html(s, heavy):
    if s["t"] == "mark":
        return f'<div class="marker {e(s["kind"])}">{e(s["text"])}</div>'
    if s["t"] == "think":
        return f'<div class="step think"><div class="lbl">thinking</div>{prose(s["text"], 900)}</div>'
    if s["t"] == "say":
        return f'<div class="step say"><div class="lbl">codex</div>{prose(s["text"])}</div>'

    state = {True: "ok", False: "bad", None: "unk"}[s.get("ok")]
    mark = {True: "✓", False: "✗", None: "?"}[s.get("ok")]
    flags = []
    if "retry_of" in s:
        flags.append(f'<span class="flag">retry of #{s["retry_of"] + 1}</span>')
    if (s.get("result_tokens") or 0) >= heavy:
        flags.append(f'<span class="flag hv">heavy ~{tok(s["result_tokens"])} tok</span>')
    if s.get("finding"):
        flags.append(f'<span class="flag bad">{e(s["finding"])}</span>')
    preview = (f'<pre class="prev">{e(s["preview"][:2000])}</pre>'
               if s.get("preview") else "")
    detail = f'<div class="sub">{e(s["detail"])}</div>' if s.get("detail") else ""
    return f"""<details class="step tool {state}">
  <summary><span class="mk">{mark}</span> <span class="tn">{e(s['tool'])}</span>
    <code>{e(clip(s['call'], 160))}</code>
    <span class="lat">{dur(s.get('latency'))}</span> {''.join(flags)}</summary>
  {detail}<div class="res">→ {e(clip(s.get('result'), 300))}</div>{preview}
</details>"""


def turn_html(t, heavy):
    parts = [f"""<section class="turn">
  <header><h2>Turn {t['n']}</h2>
    <span class="meta">{dur(t['duration'])} · {t['n_tools']} calls ·
      {e(token_line(t['tokens']))}{' · $%.2f' % t['cost'] if t.get('cost') is not None else ''}</span>
    {ribbon(t['steps'], heavy)}
  </header>"""]
    if t["prompt"]:
        parts.append(f'<div class="step you"><div class="lbl">you</div>{prose(t["prompt"], 2000)}</div>')
    for s in t["steps"]:
        parts.append(step_html(s, heavy))
    parts.append("</section>")
    return "\n".join(parts)


CSS = """
:root{--bg:#0d1117;--fg:#e6edf3;--dim:#8b949e;--line:#21262d;--card:#161b22;
--ok:#3fb950;--bad:#f85149;--unk:#d29922;--acc:#58a6ff;}
@media(prefers-color-scheme:light){:root{--bg:#fff;--fg:#1f2328;--dim:#636c76;
--line:#d8dee4;--card:#f6f8fa;--ok:#1a7f37;--bad:#cf222e;--unk:#9a6700;--acc:#0969da;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;}
.wrap{max-width:960px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:24px;margin:0 0 6px}
.sub,.meta{color:var(--dim);font-size:13px}
.tiles{display:flex;flex-wrap:wrap;gap:10px;margin:20px 0 8px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:10px 14px;min-width:120px}
.tile .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
.tile .v{font-size:19px;font-weight:600}
.tile.accent .v{color:var(--acc)}
section.turn{border:1px solid var(--line);border-radius:10px;margin:22px 0;
background:var(--card);overflow:hidden}
section.turn header{padding:12px 16px;border-bottom:1px solid var(--line)}
section.turn h2{font-size:15px;margin:0;display:inline-block}
section.turn .meta{margin-left:10px}
.ribbon{margin-top:8px;display:flex;flex-wrap:wrap;gap:2px}
.ribbon i{width:9px;height:9px;border-radius:2px;background:var(--ok);display:block}
.ribbon i.bad{background:var(--bad)}.ribbon i.unk{background:var(--unk)}
.ribbon i.hv{outline:1px solid var(--acc);outline-offset:1px}
.step{padding:10px 16px;border-bottom:1px solid var(--line)}
.step:last-child{border-bottom:none}
.lbl{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);
margin-bottom:4px}
.step.you{background:rgba(88,166,255,.07)}
.step.think{color:var(--dim);font-style:italic}
.prose{white-space:pre-wrap;overflow-wrap:anywhere}
details.tool summary{cursor:pointer;list-style:none;display:flex;gap:8px;
align-items:baseline;flex-wrap:wrap}
details.tool summary::-webkit-details-marker{display:none}
.mk{font-weight:700}
.tool.ok .mk{color:var(--ok)}.tool.bad .mk{color:var(--bad)}.tool.unk .mk{color:var(--unk)}
.tn{font-weight:600}
summary code{background:var(--bg);border:1px solid var(--line);border-radius:4px;
padding:1px 6px;font-size:12.5px;overflow-wrap:anywhere}
.lat{color:var(--dim);font-size:12px}
.flag{font-size:11px;border:1px solid var(--line);border-radius:10px;padding:0 7px;
color:var(--dim)}
.flag.hv{border-color:var(--acc);color:var(--acc)}
.flag.bad{border-color:var(--bad);color:var(--bad)}
.res{margin-top:6px;font-size:13.5px;color:var(--dim);overflow-wrap:anywhere}
pre.prev{background:var(--bg);border:1px solid var(--line);border-radius:6px;
padding:10px;overflow:auto;max-height:340px;font-size:12.5px;margin:8px 0 0}
.marker{margin:0 16px 10px;padding:6px 10px;border-left:3px solid var(--unk);
background:var(--bg);font-size:13px;color:var(--dim)}
.marker.aborted{border-left-color:var(--bad)}
.marker.subagent{border-left-color:var(--acc)}
footer{margin-top:34px;color:var(--dim);font-size:12.5px;line-height:1.7}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
"""


def render_html(d, heavy):
    tk = d["tokens"]
    all_tools = [s for t in d["turns"] for s in tools_of(t)]
    nf = sum(t["n_failed"] for t in d["turns"])
    nr = sum(t["n_retries"] for t in d["turns"])
    cost = f"${d['cost']:.2f}" if not d["unpriced_models"] else "n/a"
    unpriced = ""
    if d["unpriced_models"]:
        unpriced = (f"<p>No price configured for "
                    f"<code>{e(', '.join(d['unpriced_models']))}</code> — add rates to "
                    f"<code>{e(pricing_mod.USER_FILE)}</code>.</p>")

    turns = "\n".join(turn_html(t, heavy) for t in d["turns"])
    ident = e(d["model"]) + (f" ({e(d['effort'])})" if d.get("effort") else "")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex narrative · {e(d['title'])}</title><style>{CSS}</style></head><body>
<div class="wrap">
<h1>{e(d['title'])}</h1>
<div class="sub">{e(d['session_id'])} · {ident} · {e(d['project'])}
{' · sub-agent ' + e(d['agent_label']) if d['is_subagent'] else ''}</div>
<div class="tiles">
  <div class="tile"><div class="k">Turns</div><div class="v">{len(d['turns'])}</div></div>
  <div class="tile"><div class="k">Tool calls</div><div class="v">{len(all_tools)}</div></div>
  <div class="tile"><div class="k">Failed</div><div class="v">{nf}</div></div>
  <div class="tile"><div class="k">Retried</div><div class="v">{nr}</div></div>
  <div class="tile"><div class="k">New in</div><div class="v">{tok(tk['in'])}</div></div>
  <div class="tile"><div class="k">From cache</div><div class="v">{tok(tk['cache_read'])}</div></div>
  <div class="tile"><div class="k">Out</div><div class="v">{tok(tk['out'])}</div></div>
  <div class="tile accent"><div class="k">Est. cost</div><div class="v">{cost}</div></div>
</div>
{turns}
<footer>
<p>Turn boundaries are <strong>exact</strong>: Codex stamps <code>turn_id</code> on
every reasoning, message, tool-call and patch record, so nothing here is inferred
from ordering.</p>
<p>Cached input is a <em>subset</em> of input and reasoning output a subset of
output — neither is added on top. Per-turn spend is the delta between cumulative
<code>token_count</code> snapshots.</p>
<p>Retries are <strong>inferred</strong> (a failed call followed by a similar one),
not recorded. Result sizes are estimated from returned bytes at ~4 chars/token.
Cost is an estimate; provider billing is authoritative.</p>
{unpriced}
</footer>
</div></body></html>"""


# ===========================================================================
def resolve(session_id, project=None):
    """Find a rollout by id, prefix, or path."""
    if session_id:
        p = Path(session_id).expanduser()
        if p.is_file():
            return p

    home = DEFAULT_CODEX_HOME
    # iter_session_files yields strings, not Paths.
    candidates = [Path(p) for p in iter_session_files(home / "sessions",
                                                      home / "archived_sessions")]
    if not candidates:
        return None

    if session_id:
        needle = session_id.lower()
        hits = [p for p in candidates if needle in p.stem.lower()]
        if not hits:
            return None
        return max(hits, key=lambda p: p.stat().st_mtime)

    if project:
        target = str(Path(project).expanduser().resolve())
        scoped = []
        for path in candidates:
            for record in read_jsonl(path):
                if record.get("type") == "session_meta":
                    if (record.get("payload") or {}).get("cwd") == target:
                        scoped.append(path)
                    break
        if scoped:
            candidates = scoped
    return max(candidates, key=lambda p: p.stat().st_mtime)


def main():
    ap = argparse.ArgumentParser(
        description="Read a Codex session as a story: prompt → tools → reply",
        formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("session_id", nargs="?",
                    help="Session id/prefix, or a path to a rollout JSONL")
    ap.add_argument("--latest", action="store_true", help="Most recent session")
    ap.add_argument("--project", help="Restrict --latest to this cwd")
    ap.add_argument("--text", action="store_true",
                    help="Render in the terminal instead of HTML")
    ap.add_argument("--out-file", help="Write to this file")
    ap.add_argument("--no-open", action="store_true",
                    help="Write the HTML but do not open a browser")
    ap.add_argument("--pricing-file", help="Extra pricing JSON")
    args = ap.parse_args()

    get_pricing(args.pricing_file)

    project = args.project
    if not args.session_id and not args.latest and not project:
        project = str(Path.cwd())
    path = resolve(args.session_id, project)
    if not path:
        scope = f" matching {args.session_id!r}" if args.session_id else ""
        print(f"No Codex session found{scope}.", file=sys.stderr)
        return 1

    d = build(path)
    if not d["turns"]:
        print(f"No turns found in {path}", file=sys.stderr)
        return 1
    heavy = calibrate_heavy(d)

    if args.text:
        print(render_text(d, heavy))
        return 0

    out = Path(args.out_file).expanduser() if args.out_file else Path(
        tempfile.gettempdir()) / f"codex-narrative-{d['session_id'][:8]}.html"
    out.write_text(render_html(d, heavy), encoding="utf-8")
    print(f"Wrote {out}")
    if not args.no_open:
        subprocess.run(["open", str(out)], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
