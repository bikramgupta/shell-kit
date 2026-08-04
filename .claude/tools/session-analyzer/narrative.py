#!/usr/bin/env python3
"""
Claude Code Session Narrative
=============================

Reconstructs a session as a human-readable story: prompt -> reasoning ->
tool calls -> what each call returned -> reply, turn by turn.

Where the data comes from (all of it already sits in the transcript JSONL;
none of it needs extra logging):

  promptId            every entry from one user prompt shares it -> exact turn
                      boundaries. Assistant entries carry no promptId, so they
                      resolve through the parentUuid chain to their user ancestor.
  toolUseResult       the STRUCTURED result, shaped per tool (Bash: stdout/stderr,
                      Read: file+numLines, Edit/Write: structuredPatch). Degrades
                      to a plain "Error: ..." string when the call failed.
  is_error            on the tool_result content block -> reliable pass/fail.
  isMeta              marks system-injected user entries, so they are not
                      mistaken for things the human actually typed.
  message.usage       per-request input / cache-write / cache-read / output tokens.
  aiTitle             Claude Code's own generated session headline.

Retries are NOT recorded anywhere; they are inferred (see infer_retries).

Usage:
    narrative.py                       # latest session in this project, opens HTML
    narrative.py <session-id>          # a specific session
    narrative.py <path/to.jsonl>       # a transcript by path
    narrative.py --text                # terminal rendering instead of HTML
    narrative.py --out-file report.html
"""
import argparse
import html
import json
import math
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parser import find_session_files  # noqa: E402  (session-id resolution)

TOK_PER_CHAR = 0.25          # result bytes -> approx tokens
HEAVY_FLOOR = 1200           # a "heavy" result is at least this many tokens
# Estimated USD per 1M tokens. Estimates only — /cost is authoritative.
PRICE = {"in": 5.0, "cache_write": 6.25, "cache_read": 0.5, "out": 25.0}
TOKEN_KEYS = ("in", "cache_write", "cache_read", "out")


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
    return f"{m}m {r:02d}s" if r else f"{m}m"


def tok(n):
    if n is None:
        return "-"
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1000:
        return f"{n/1000:.1f}K"
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


def cost_of(t):
    return sum(t[k] * PRICE[k] for k in TOKEN_KEYS) / 1e6


def zero_tokens():
    return {k: 0 for k in TOKEN_KEYS}


def add_tokens(a, b):
    for k in TOKEN_KEYS:
        a[k] += b[k]
    return a


def usage_key(entry):
    """Identity of the API response an assistant entry belongs to.

    One response is written as SEVERAL JSONL entries — one per content block
    (thinking, text, tool_use) — and every one of them repeats the same usage
    object. Summing per entry double- or triple-counts the whole session, so
    usage must be counted once per message id.
    """
    msg = entry.get("message") or {}
    return msg.get("id") or entry.get("requestId") or entry.get("uuid")


# ===========================================================================
# Transcript loading
# ===========================================================================
def load(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def ts(entry):
    return entry.get("timestamp") or ""


def secs(a, b):
    try:
        da = datetime.fromisoformat(a.replace("Z", "+00:00"))
        db = datetime.fromisoformat(b.replace("Z", "+00:00"))
        return (db - da).total_seconds()
    except Exception:
        return None


def segment(entries):
    """Group entries into turns. Assistant entries have no promptId, so they
    resolve to the nearest user ancestor through the parentUuid chain."""
    by_uuid = {e["uuid"]: e for e in entries if e.get("uuid")}
    memo = {}

    def resolve(e, depth=0):
        u = e.get("uuid")
        if u in memo:
            return memo[u]
        if e.get("promptId"):
            memo[u] = e["promptId"]
            return memo[u]
        p = e.get("parentUuid")
        memo[u] = resolve(by_uuid[p], depth + 1) if (
            p and p in by_uuid and depth < 500) else None
        return memo[u]

    turns, order = {}, []
    for e in entries:
        if not e.get("uuid"):
            continue
        pid = resolve(e)
        if not pid:
            continue
        if pid not in turns:
            turns[pid] = []
            order.append(pid)
        turns[pid].append(e)
    return [(pid, turns[pid]) for pid in order]


SLASH = re.compile(r"<command-name>/?([\w-]+)</command-name>")


def user_text(entries):
    """The first thing the human actually typed in this turn."""
    for e in entries:
        if e.get("type") != "user" or e.get("isMeta"):
            continue
        c = e.get("message", {}).get("content")
        raw = ""
        if isinstance(c, str):
            raw = c
        elif isinstance(c, list):
            for it in c:
                if isinstance(it, str):
                    raw = it
                    break
                if isinstance(it, dict) and it.get("type") == "text":
                    raw = it.get("text", "")
                    break
        raw = (raw or "").strip()
        if not raw:
            continue
        m = SLASH.search(raw)
        if m:
            a = re.search(r"<command-args>(.*?)</command-args>", raw, re.S)
            arg = (a.group(1).strip() if a else "")
            return (f"/{m.group(1)}" + (f" {arg}" if arg else ""), "command")
        if raw.startswith("<") and raw.endswith(">"):
            continue
        return (raw, "human")
    return ("", "continuation")


# ===========================================================================
# Per-tool renderers: (input, toolUseResult, is_error) -> readable summary
# ===========================================================================
def _t(s, n=120):
    return clip(s, n)


def r_bash(inp, res, err):
    out = {"call": _t(inp.get("command", ""), 200)}
    if err or isinstance(res, str):
        txt = res if isinstance(res, str) else ""
        m = re.match(r"Error: Exit code (\d+)", txt)
        out["exit_code"] = int(m.group(1)) if m else None
        out["finding"] = _t(txt.replace("Error:", "").strip(), 200)
        return out
    if isinstance(res, dict):
        so, se = res.get("stdout") or "", res.get("stderr") or ""
        lines = so.count("\n") + (1 if so else 0)
        out.update(exit_code=0, bytes=len(so) + len(se), lines=lines,
                   interrupted=bool(res.get("interrupted")))
        head = _t(so.strip().split("\n")[0], 110) if so.strip() else "(no output)"
        out["finding"] = f"{lines} line(s)" + (f" — {head}" if so.strip() else "")
        if se.strip():
            out["finding"] += f" | stderr: {_t(se, 70)}"
    return out


def r_read(inp, res, err):
    out = {"call": inp.get("file_path", "")}
    if err or isinstance(res, str):
        out["finding"] = _t(res if isinstance(res, str) else "failed", 160)
        return out
    if isinstance(res, dict):
        f = res.get("file") or {}
        if res.get("type") == "image" or "base64" in f:
            # Images are expensive and have no line count — report what costs.
            b64 = f.get("base64") or ""
            dims = f.get("dimensions") or {}
            w, h = dims.get("displayWidth"), dims.get("displayHeight")
            what = f.get("type") or "image"
            out.update(bytes=len(b64), lines=0)
            out["finding"] = (what + (f" {w}x{h}" if w and h else "")
                              + f", {len(b64):,} base64 chars sent")
            return out
        content = f.get("content") or ""
        n = f.get("numLines") or content.count("\n")
        off, lim = inp.get("offset"), inp.get("limit")
        total = f.get("totalLines")
        out.update(lines=n, bytes=len(content))
        out["finding"] = (f"read {n} line(s)"
                          + (f" from line {off}" if off else "")
                          + (f" of {total}" if total and n and total > n else ""))
    return out


def _patch_counts(patch):
    add = sum(1 for h in patch for l in h.get("lines", []) if l.startswith("+"))
    rm = sum(1 for h in patch for l in h.get("lines", []) if l.startswith("-"))
    return add, rm


def r_edit(inp, res, err):
    out = {"call": inp.get("file_path", "")}
    if err or isinstance(res, str):
        out["finding"] = _t(res if isinstance(res, str) else "failed", 160)
        return out
    if isinstance(res, dict):
        patch = res.get("structuredPatch") or []
        add, rm = _patch_counts(patch)
        out.update(added=add, removed=rm)
        out["finding"] = f"+{add} −{rm} across {len(patch)} hunk(s)"
    return out


def r_write(inp, res, err):
    out = {"call": inp.get("file_path", "")}
    if err or isinstance(res, str):
        out["finding"] = _t(res if isinstance(res, str) else "failed", 160)
        return out
    content = (res or {}).get("content", "") if isinstance(res, dict) else ""
    out.update(bytes=len(content), lines=content.count("\n"))
    out["finding"] = f"wrote {out['lines']} line(s), {len(content):,} bytes"
    return out


def r_generic(inp, res, err):
    key = next((k for k in ("pattern", "query", "prompt", "url", "path",
                            "command", "file_path", "description") if k in inp), None)
    out = {"call": _t(inp.get(key) if key else json.dumps(inp), 160)}
    body = res if isinstance(res, str) else (json.dumps(res) if res else "")
    out["bytes"] = len(body)
    out["finding"] = _t(body, 160) if body else "(no structured result)"
    return out


RENDERERS = {"Bash": r_bash, "Read": r_read, "Edit": r_edit, "Write": r_write}


def preview(tur, raw):
    """Readable full result body for the expanded view."""
    if isinstance(tur, str):
        return tur
    if isinstance(tur, dict):
        if "stdout" in tur:
            so, se = tur.get("stdout") or "", tur.get("stderr") or ""
            return so + (f"\n--- stderr ---\n{se}" if se.strip() else "")
        if "file" in tur:
            return (tur["file"] or {}).get("content", "") or ""
        if "structuredPatch" in tur:
            return "\n".join(l for h in tur["structuredPatch"]
                             for l in h.get("lines", []))
        return json.dumps(tur, indent=2)
    if isinstance(raw, list):
        return "\n".join(x.get("text", "") for x in raw if isinstance(x, dict))
    return str(raw or "")


# ===========================================================================
# Retry inference (heuristic — nothing in the transcript marks a retry)
# ===========================================================================
EXACT_MATCH_TOOLS = {"Read", "Edit", "Write", "NotebookEdit"}


def infer_retries(tools):
    """A failed call followed by the same tool on the same target.

    For file tools the target is the path — an exact match or nothing, since
    sibling paths share long prefixes and would fool a similarity score.
    Only free-form tools (Bash, Grep, ...) fall back to fuzzy matching.
    """
    for i, s in enumerate(tools):
        if s.get("ok") is not False:
            continue
        for j in range(i + 1, min(i + 5, len(tools))):
            nxt = tools[j]
            if nxt["tool"] != s["tool"] or "retry_of" in nxt:
                continue
            if s["tool"] in EXACT_MATCH_TOOLS:
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
def build(path):
    entries = load(path)

    title = last_prompt = project = branch = ""
    for e in entries:
        if e.get("type") == "ai-title":
            title = e.get("aiTitle") or title
        elif e.get("type") == "last-prompt":
            last_prompt = e.get("lastPrompt") or last_prompt
        project = e.get("cwd") or project
        branch = e.get("gitBranch") or branch

    turns = []
    counted = set()          # message ids whose usage has already been added
    for idx, (pid, es) in enumerate(segment(entries), 1):
        text, kind = user_text(es)
        pending, steps = {}, []
        tokens = zero_tokens()
        turn_model = None
        requests = 0

        for e in es:
            et = e.get("type")
            msg = e.get("message") or {}
            if et == "assistant":
                turn_model = turn_model or msg.get("model")
                key = usage_key(e)
                if key not in counted:
                    counted.add(key)
                    requests += 1
                    u = msg.get("usage") or {}
                    tokens["in"] += u.get("input_tokens", 0)
                    tokens["out"] += u.get("output_tokens", 0)
                    tokens["cache_write"] += u.get("cache_creation_input_tokens", 0)
                    tokens["cache_read"] += u.get("cache_read_input_tokens", 0)

            c = msg.get("content")
            if not isinstance(c, list):
                if isinstance(c, str) and et == "assistant" and c.strip():
                    steps.append({"t": "say", "text": c.strip(), "ts": ts(e)})
                continue

            for it in c:
                if not isinstance(it, dict):
                    continue
                t = it.get("type")
                if t == "thinking":
                    th = (it.get("thinking") or "").strip()
                    if th:
                        steps.append({"t": "think", "text": th, "ts": ts(e)})
                elif t == "text" and et == "assistant":
                    tx = (it.get("text") or "").strip()
                    if tx:
                        steps.append({"t": "say", "text": tx, "ts": ts(e)})
                elif t == "tool_use":
                    pending[it.get("id")] = (it.get("name"), it.get("input"), ts(e))
                elif t == "tool_result":
                    name, inp, t0 = pending.pop(it.get("tool_use_id"),
                                                ("unknown", {}, ts(e)))
                    err = bool(it.get("is_error"))
                    tur = e.get("toolUseResult")
                    raw = it.get("content")
                    d = RENDERERS.get(name, r_generic)(inp or {}, tur, err)
                    nbytes = d.get("bytes") or (len(json.dumps(raw)) if raw else 0)
                    d.update({
                        "t": "tool", "tool": name, "ok": not err, "ts": t0,
                        "latency": secs(t0, ts(e)),
                        "result_bytes": nbytes,
                        "result_tokens": int(nbytes * TOK_PER_CHAR),
                        "input_full": json.dumps(inp, indent=2)[:3000],
                        "output_full": preview(tur, raw)[:3000],
                    })
                    steps.append(d)

        for tid, (name, inp, t0) in pending.items():
            d = RENDERERS.get(name, r_generic)(inp or {}, None, False)
            d.update({"t": "tool", "tool": name, "ok": None, "ts": t0,
                      "latency": None, "result_bytes": 0, "result_tokens": 0,
                      "input_full": json.dumps(inp, indent=2)[:3000],
                      "output_full": "",
                      "finding": "no result recorded (interrupted or still running)"})
            steps.append(d)

        tools = [s for s in steps if s["t"] == "tool"]
        infer_retries(tools)

        t_start = min((s["ts"] for s in steps if s.get("ts")), default=ts(es[0]))
        t_end = max((s["ts"] for s in steps if s.get("ts")), default=ts(es[-1]))
        turns.append({
            "n": idx, "prompt_id": pid, "prompt": text, "prompt_kind": kind,
            "model": turn_model, "start": t_start, "end": t_end,
            "duration": secs(t_start, t_end), "steps": steps, "tokens": tokens,
            "api_requests": requests, "n_tools": len(tools),
            "n_failed": sum(1 for s in tools if s.get("ok") is False),
            "n_retries": sum(1 for s in tools if "retry_of" in s),
        })

    annotate_model_changes(turns)

    all_ts = [ts(e) for e in entries if ts(e)]
    session_tokens = zero_tokens()
    for t in turns:
        add_tokens(session_tokens, t["tokens"])

    return {
        "session_id": Path(path).stem,
        "title": title or clip(last_prompt, 70) or "Untitled session",
        "model": next((t["model"] for t in reversed(turns) if t["model"]), ""),
        "project": project, "branch": branch, "file": str(path),
        "start": min(all_ts) if all_ts else "",
        "end": max(all_ts) if all_ts else "",
        "turns": turns,
        "tokens": session_tokens,
        "cost": cost_of(session_tokens),
    }


def annotate_model_changes(turns):
    """`/model` records no argument, but the model it selected is visible as the
    model of the next turn that actually called the API."""
    current = None
    for i, t in enumerate(turns):
        if t["prompt_kind"] == "command" and t["prompt"].startswith("/model"):
            nxt = next((x["model"] for x in turns[i + 1:] if x["model"]), None)
            t["model_from"], t["model_to"] = current, nxt
        if t["model"]:
            current = t["model"]


def calibrate_heavy(d):
    """'Heavy' is judged against this session's own p90, with an absolute floor."""
    v = sorted((s.get("result_tokens") or 0)
               for t in d["turns"] for s in tools_of(t))
    return max(HEAVY_FLOOR, v[int(len(v) * 0.9)]) if v else HEAVY_FLOOR


def tools_of(turn):
    return [s for s in turn["steps"] if s["t"] == "tool"]


def turn_kind(t):
    if t["prompt_kind"] == "command":
        return "command"
    if not tools_of(t) and not any(s["t"] == "say" for s in t["steps"]):
        return "superseded"
    return "work"


def command_label(t):
    """What a slash-command turn actually changed."""
    if t["prompt"].startswith("/model"):
        to, frm = t.get("model_to"), t.get("model_from")
        if to and frm and to != frm:
            return f"model {frm} → {to}"
        if to:
            return f"model set to {to}"
        return "model unchanged"
    if t["prompt"].startswith("/effort"):
        arg = t["prompt"].split(" ", 1)[1] if " " in t["prompt"] else ""
        return f"reasoning effort → {arg}" if arg else "reasoning effort changed"
    return "setting changed"


# ===========================================================================
# Terminal surface
# ===========================================================================
SPARK = "▁▂▃▄▅▆▇█"


def spark(vals):
    if not vals:
        return ""
    hi = max(vals) or 1
    return "".join(SPARK[min(7, int(math.log1p(v) / math.log1p(hi) * 7))]
                   for v in vals)


def wrap(text, width, indent):
    out = []
    for para in str(text).split("\n"):
        if not para.strip():
            continue
        line = ""
        for w in para.split():
            while len(w) > width:
                if line:
                    out.append(indent + line)
                    line = ""
                out.append(indent + w[:width - 1] + "\\")
                w = w[width - 1:]
            if len(line) + len(w) + 1 > width:
                out.append(indent + line)
                line = w
            else:
                line = f"{line} {w}".strip()
        if line:
            out.append(indent + line)
    return out


def token_line(t):
    """What the API actually had to process this turn.

    `input_tokens` alone is misleading: under prompt caching it is only the
    residual after the last cache breakpoint (often single digits). A new
    prompt and any freshly added context are billed as cache WRITES, so
    "new" = input + cache_write is the number that answers "how much did
    this turn add", and cache_read is the conversation being re-sent cheaply.
    """
    return (f"{tok(t['in'] + t['cache_write'])} new · "
            f"{tok(t['cache_read'])} from cache · {tok(t['out'])} out")


def render_text(d, heavy):
    L, W = [], 92
    tl_all = [s for t in d["turns"] for s in tools_of(t)]
    nf = sum(t["n_failed"] for t in d["turns"])
    nr = sum(t["n_retries"] for t in d["turns"])
    tk = d["tokens"]

    L.append("━" * W)
    L.append(f"  {d['title']}")
    L.append(f"  {d['session_id'][:8]} · {d['model']} · "
             f"{Path(d['project']).name} · {d['branch']}")
    L.append(f"  {len(d['turns'])} turns · {len(tl_all)} tool calls · "
             f"{nf} failed · {nr} retried")
    L.append(f"  {tok(tk['in'] + tk['cache_write'])} new in "
             f"({tok(tk['in'])} uncached + {tok(tk['cache_write'])} cache-write) · "
             f"{tok(tk['cache_read'])} from cache · {tok(tk['out'])} out "
             f"· ~${d['cost']:.2f}")
    L.append("━" * W)
    L.append("")

    for t in d["turns"]:
        kind = turn_kind(t)
        if kind == "command":
            L.append(f"  {t['n']:02d}  {t['prompt']:<24} {command_label(t)}")
            L.append("")
            continue
        if kind == "superseded":
            L.append(f"TURN {t['n']:02d}  ·  prompt superseded — no work recorded")
            L.extend(wrap(clip(t["prompt"], 150), 78, "│  "))
            L.append("")
            L.append("─" * W)
            L.append("")
            continue

        tl = tools_of(t)
        head = (f"TURN {t['n']:02d}  ·  {dur(t['duration'])}  ·  {len(tl)} calls"
                + (f"  ·  {t['n_failed']} failed" if t["n_failed"] else "")
                + (f"  ·  {t['n_retries']} retried" if t["n_retries"] else ""))
        bar = spark([s.get("result_tokens", 0) for s in tl])
        L.append(head + "  " + bar.rjust(max(0, W - len(head) - 2)))
        L.append(f"           {token_line(t['tokens'])}")
        L.append("│")

        if t["prompt"]:
            L.append("├─ YOU")
            L.extend(wrap(clip(t["prompt"], 340), 78, "│  "))
            L.append("│")

        for s in t["steps"]:
            if s["t"] == "think":
                L.append(f"├─ thinking  ({len(s['text']):,} chars)")
                L.extend(wrap(clip(s["text"], 170), 74, "│  ┆ "))
                L.append("│")
            elif s["t"] == "say":
                L.append("├─ CLAUDE")
                L.extend(wrap(clip(s["text"], 340), 78, "│  "))
                L.append("│")
            else:
                mark = "✗" if s["ok"] is False else ("↻" if "retry_of" in s else "─")
                hv = "◆" if (s.get("result_tokens") or 0) >= heavy else " "
                call = re.sub(r"^/Users/[^/]+/", "~/", str(s.get("call", "")))
                if len(call) > 43:
                    call = "…" + call[-42:]
                right = (f"{tok(s.get('result_tokens'))+'t':>7} "
                         f"{dur(s.get('latency')):>7}")
                L.append(f"├{mark} {s['tool']:<7} {call:<43}{hv}{right}")
                if s.get("finding"):
                    L.append(f"│    └ {clip(s['finding'], 78)}")
                if "retry_of" in s:
                    L.append(f"│      ↑ retry of call {s['retry_of']+1} in this turn")
        L.append("")
        L.append("─" * W)
        L.append("")

    L.append("Tokens are counted once per API response (one response spans "
             "several transcript entries).")
    L.append("Under prompt caching a new prompt is billed as a cache WRITE, so "
             "'uncached' stays tiny.")
    L.append("Result sizes are estimated from returned bytes; cost is an "
             "estimate — /cost is authoritative.")
    return "\n".join(L)


# ===========================================================================
# HTML surface
# ===========================================================================
def e(s):
    return html.escape(str(s if s is not None else ""))


def prose(text, limit=1400):
    """Human/agent prose: keep paragraph breaks, they carry the structure."""
    t = str(text or "")
    if len(t) > limit:
        cut = t[:limit]
        sp = cut.rfind(" ")
        t = (cut[:sp] if sp > limit * 0.5 else cut).rstrip(" ,;:-") + "…"
    t = e(t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    return "".join(f"<p>{ln}</p>" for ln in t.split("\n") if ln.strip())


def ribbon(steps, heavy, cls="ribbon"):
    tl = [s for s in steps if s["t"] == "tool"]
    if not tl:
        return f'<div class="{cls} empty"></div>'
    hi = max((s.get("result_tokens") or 0) for s in tl) or 1
    bars = []
    for s in tl:
        v = s.get("result_tokens") or 0
        h = 12 + int(math.log1p(v) / math.log1p(hi) * 88)
        state = ("fail" if s["ok"] is False else
                 "retry" if "retry_of" in s else
                 "heavy" if v >= heavy else "ok")
        bars.append(f'<i class="b {state}" style="height:{h}%" '
                    f'title="{e(s["tool"])} · {tok(v)} tokens returned"></i>')
    return f'<div class="{cls}">{"".join(bars)}</div>'


def step_html(s, heavy):
    if s["t"] == "think":
        return (f'<li class="step think"><span class="lane"><i class="tick"></i></span>'
                f'<details><summary><span class="kind">thinking</span>'
                f'<span class="tsummary">{e(clip(s["text"], 150))}</span>'
                f'<span class="num">{len(s["text"]):,} ch</span></summary>'
                f'<div class="pane">{prose(s["text"], 3000)}</div></details></li>')
    if s["t"] == "say":
        return (f'<li class="step say"><span class="lane"></span>'
                f'<div class="bubble"><span class="kind">Claude</span>'
                f'{prose(s["text"])}</div></li>')

    v = s.get("result_tokens") or 0
    state = ("fail" if s["ok"] is False else
             "retry" if "retry_of" in s else "ok")
    is_heavy = v >= heavy and state == "ok"
    mark = {"fail": "✗", "retry": "↻", "ok": ""}[state]
    call = re.sub(r"^/Users/[^/]+/", "~/", str(s.get("call", "")))
    note = ""
    if "retry_of" in s:
        note = f'<span class="note">retry of call {s["retry_of"]+1}</span>'
    elif is_heavy:
        note = '<span class="note heavy-note">heavy result</span>'
    return f'''<li class="step tool {state}{" heavy" if is_heavy else ""}" \
data-state="{state}" data-heavy="{int(is_heavy)}">
<span class="lane"><i class="tick"></i></span>
<details><summary>
  <span class="mark">{mark}</span>
  <span class="tname">{e(s["tool"])}</span>
  <span class="call">{e(call)}</span>
  <span class="n1"><em>{e(clip(s.get("finding", ""), 64))}</em>{note}</span>
  <span class="num n2" title="estimated size of what this call returned">{tok(v)}t</span>
  <span class="num n3">{dur(s.get("latency"))}</span>
</summary>
<div class="pane split">
  <div><h5>sent</h5><pre>{e(s.get("input_full", ""))}</pre></div>
  <div><h5>returned{" · error" if state == "fail" else ""}</h5>
       <pre>{e(s.get("output_full", ""))}</pre></div>
</div></details></li>'''


def turn_html(t, heavy):
    kind = turn_kind(t)
    if kind == "command":
        return f'''<section class="turn aside" id="t{t['n']}" data-turn="{t['n']}">
  <span class="ano">{t['n']:02d}</span><span class="acmd">{e(t["prompt"])}</span>
  <span class="alab">{e(command_label(t))}</span></section>'''
    if kind == "superseded":
        return f'''<section class="turn superseded" id="t{t['n']}" data-turn="{t['n']}">
  <header class="thead"><span class="tno">{t['n']:02d}</span>
    <span class="tstat">prompt superseded — no work recorded</span></header>
  <div class="prompt command">{prose(clip(t["prompt"], 220))}</div>
</section>'''

    tl = tools_of(t)
    flags = []
    if t["n_failed"]:
        flags.append(f'<span class="flag fail">{t["n_failed"]} failed</span>')
    if t["n_retries"]:
        flags.append(f'<span class="flag retry">{t["n_retries"]} retried</span>')
    hv = sum(1 for s in tl if (s.get("result_tokens") or 0) >= heavy)
    if hv:
        flags.append(f'<span class="flag heavy">{hv} heavy</span>')

    tk = t["tokens"]
    new_in = tk["in"] + tk["cache_write"]
    toks = (f'<span class="tk" title="{tok(tk["cache_write"])} billed as cache '
            f'writes + {tok(tk["in"])} uncached — your prompt and any newly '
            f'added context land here"><b>{tok(new_in)}</b> new in</span>'
            f'<span class="tk" title="conversation re-sent from cache at 0.1x '
            f'rate"><b>{tok(tk["cache_read"])}</b> from cache</span>'
            f'<span class="tk"><b>{tok(tk["out"])}</b> out</span>'
            f'<span class="tk"><b>{t["api_requests"]}</b> requests</span>')

    steps = "".join(step_html(s, heavy) for s in t["steps"])
    prompt = (f'<div class="prompt"><span class="kind">You</span>'
              f'{prose(t["prompt"], 900)}</div>' if t["prompt"] else "")
    return f'''<section class="turn" id="t{t['n']}" data-turn="{t['n']}">
  <header class="thead">
    <span class="tno">{t['n']:02d}</span>
    <span class="tstat">{dur(t['duration'])}</span>
    <span class="tstat">{len(tl)} calls</span>
    {"".join(flags)}
    {ribbon(t["steps"], heavy, "ribbon turn-ribbon")}
  </header>
  <div class="tokens">{toks}</div>
  {prompt}
  <ol class="ledger">{steps}</ol>
</section>'''


def render_html(d, heavy):
    tk = d["tokens"]
    tl_all = [s for t in d["turns"] for s in tools_of(t)]
    nf = sum(t["n_failed"] for t in d["turns"])
    nr = sum(t["n_retries"] for t in d["turns"])
    n_heavy = sum(1 for s in tl_all if (s.get("result_tokens") or 0) >= heavy)
    real = [t for t in d["turns"] if turn_kind(t) == "work"]

    mx = max((len(tools_of(t)) for t in d["turns"]), default=1) or 1
    seg = []
    for t in d["turns"]:
        n = len(tools_of(t))
        g = 1 + math.sqrt(n / mx) * 6
        cl = "seg" + (" has-fail" if t["n_failed"] else "") + (" quiet" if not n else "")
        seg.append(f'<a class="{cl}" style="flex-grow:{g:.2f}" href="#t{t["n"]}" '
                   f'title="Turn {t["n"]} · {n} calls">'
                   f'{ribbon(t["steps"], heavy, "ribbon mini")}'
                   f'<span class="segno">{t["n"]:02d}</span></a>')

    turns = "".join(turn_html(t, heavy) for t in d["turns"])

    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Narrative · {e(d["session_id"][:8])}</title>
<style>
:root{{
  --ground:#0A0E13; --panel:#111821; --panel2:#151E28; --rule:#1E2A36; --rule2:#27353F;
  --ink:#D8E0E8; --dim:#6C7E8F; --dimmer:#41505D;
  --human:#E8A33D; --tool:#3FB8A0; --fail:#E0524F; --think:#8B7BD8;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--ground);color:var(--ink);font:14px/1.6 var(--mono);
  -webkit-font-smoothing:antialiased}}
.wrap{{max-width:1180px;margin:0 auto;padding:0 24px 120px}}

.mast{{padding:56px 0 28px;border-bottom:1px solid var(--rule)}}
.eyebrow{{font-size:11px;letter-spacing:.34em;text-transform:uppercase;color:var(--dim)}}
h1{{font:600 clamp(24px,3.4vw,40px)/1.15 var(--mono);letter-spacing:-.02em;margin:14px 0 0;
  max-width:22ch;text-wrap:balance}}
.subline{{margin-top:14px;color:var(--dim);font-size:12.5px;display:flex;flex-wrap:wrap;gap:0 10px}}
.subline b{{color:var(--ink);font-weight:500}}
.subline span:not(:last-child):after{{content:"·";margin-left:10px;color:var(--dimmer)}}

.strip{{display:flex;gap:3px;align-items:flex-end;height:76px;margin:30px 0 8px}}
.seg{{display:flex;flex-direction:column;justify-content:flex-end;gap:5px;min-width:14px;
  padding:5px 4px 0;background:var(--panel);border:1px solid var(--rule);border-radius:2px;
  text-decoration:none;transition:background .15s,border-color .15s}}
.seg:hover{{background:var(--panel2);border-color:var(--rule2)}}
.seg:focus-visible{{outline:2px solid var(--tool);outline-offset:2px}}
.seg.has-fail{{border-color:color-mix(in srgb,var(--fail) 45%,var(--rule))}}
.seg.quiet{{opacity:.4}}
.segno{{font-size:9px;color:var(--dimmer);text-align:center;letter-spacing:.06em}}
.striplab{{display:flex;justify-content:space-between;font-size:10.5px;color:var(--dimmer);
  letter-spacing:.16em;text-transform:uppercase}}

.ribbon{{display:flex;align-items:flex-end;gap:1px;height:100%;min-height:20px}}
.ribbon .b{{flex:1;min-width:2px;background:var(--tool);opacity:.65;border-radius:1px 1px 0 0}}
.ribbon .b.fail{{background:var(--fail);opacity:1}}
.ribbon .b.retry{{background:var(--human);opacity:1}}
.ribbon .b.heavy{{background:var(--think);opacity:.95}}
.ribbon.mini{{height:34px}}
.ribbon.turn-ribbon{{margin-left:auto;width:min(280px,34%);height:26px}}

.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));
  border:1px solid var(--rule);border-radius:3px;margin-top:26px;overflow:hidden}}
.tile{{padding:14px 16px;border-right:1px solid var(--rule)}}
.tile:last-child{{border-right:none}}
.tile .k{{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim)}}
.tile .v{{font-size:21px;font-weight:600;letter-spacing:-.01em;margin-top:5px}}
.tile .sub{{font-size:10.5px;color:var(--dimmer);margin-top:3px}}
.tile.warn .v{{color:var(--fail)}}
.tile.accent .v{{color:var(--think)}}
.tiles.money{{margin-top:10px}}
.tiles.money .v{{font-size:18px}}

.filters{{display:flex;gap:7px;flex-wrap:wrap;margin:26px 0 0;
  position:sticky;top:0;background:var(--ground);padding:14px 0;z-index:5;
  border-bottom:1px solid var(--rule)}}
.chip{{font:inherit;font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--dim);background:none;border:1px solid var(--rule);border-radius:2px;
  padding:6px 12px;cursor:pointer;transition:color .15s,border-color .15s}}
.chip:hover{{color:var(--ink);border-color:var(--rule2)}}
.chip[aria-pressed="true"]{{color:var(--ground);background:var(--ink);border-color:var(--ink)}}
.chip:focus-visible{{outline:2px solid var(--tool);outline-offset:2px}}
.legend{{font-size:10.5px;color:var(--dimmer);padding:10px 0 0;letter-spacing:.04em}}

.turn{{padding:44px 0 8px;border-bottom:1px solid var(--rule)}}
.turn.aside{{display:flex;align-items:baseline;gap:12px;padding:9px 0;
  font-size:11.5px;color:var(--dimmer)}}
.ano{{font-weight:600}} .acmd{{color:var(--dim)}}
.alab{{margin-left:auto;letter-spacing:.14em;text-transform:uppercase;font-size:10px;
  color:var(--dim)}}
.turn.superseded{{padding:24px 0 14px}}
.thead{{display:flex;align-items:center;gap:14px;flex-wrap:wrap}}
.tno{{font-size:30px;font-weight:600;letter-spacing:-.03em;color:var(--dimmer)}}
.tstat{{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}}
.flag{{font-size:10px;letter-spacing:.1em;text-transform:uppercase;padding:3px 8px;
  border:1px solid;border-radius:2px}}
.flag.fail{{color:var(--fail);border-color:color-mix(in srgb,var(--fail) 40%,transparent)}}
.flag.retry{{color:var(--human);border-color:color-mix(in srgb,var(--human) 40%,transparent)}}
.flag.heavy{{color:var(--think);border-color:color-mix(in srgb,var(--think) 40%,transparent)}}
.tokens{{display:flex;gap:18px;flex-wrap:wrap;margin:10px 0 0;font-size:11px;
  letter-spacing:.1em;text-transform:uppercase;color:var(--dimmer)}}
.tk b{{color:var(--dim);font-weight:600;letter-spacing:0}}

.prompt,.bubble{{font-family:var(--sans);font-size:15px;line-height:1.62}}
.prompt{{margin:18px 0 8px;padding:16px 20px;background:var(--panel);
  border-left:2px solid var(--human);border-radius:0 3px 3px 0}}
.prompt.command{{border-left-color:var(--dimmer);color:var(--dim);
  font-family:var(--mono);font-size:13px}}
.prompt p,.bubble p{{margin:0 0 9px}}
.prompt p:last-child,.bubble p:last-child{{margin:0}}
.kind{{display:block;font:500 10px/1 var(--mono);letter-spacing:.22em;text-transform:uppercase;
  color:var(--dim);margin-bottom:9px}}
.prompt .kind{{color:var(--human)}}

.ledger{{list-style:none;margin:8px 0 0;padding:0}}
.step{{display:grid;grid-template-columns:26px 1fr;align-items:stretch}}
.lane{{position:relative}}
.lane:before{{content:"";position:absolute;left:9px;top:0;bottom:0;width:1px;
  background:var(--rule2)}}
.step:first-child .lane:before{{top:14px}}
.step:last-child .lane:before{{bottom:calc(100% - 14px)}}
.tick{{position:absolute;left:6px;top:11px;width:7px;height:7px;border-radius:50%;
  background:var(--ground);border:1.5px solid var(--tool)}}
.step.fail .tick{{border-color:var(--fail);background:var(--fail)}}
.step.retry .tick{{border-color:var(--human)}}
.step.heavy .tick{{border-color:var(--think)}}
.step.think .tick{{border-color:var(--think)}}
.step summary{{display:grid;
  grid-template-columns:14px 60px minmax(0,1.1fr) minmax(0,1fr) 58px 54px;
  gap:10px;align-items:baseline;padding:5px 8px;cursor:pointer;list-style:none;
  border-radius:2px}}
.step summary::-webkit-details-marker{{display:none}}
.step summary:hover{{background:var(--panel)}}
.step summary:focus-visible{{outline:1px solid var(--tool);outline-offset:-1px}}
.mark{{color:var(--dimmer);font-size:12px}}
.step.fail .mark{{color:var(--fail)}} .step.retry .mark{{color:var(--human)}}
.tname{{font-size:12.5px;color:var(--ink);font-weight:500}}
.step.fail .tname{{color:var(--fail)}}
.call{{font-size:12.5px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}}
.num{{font-size:11.5px;color:var(--dimmer);text-align:right;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}}
.n1{{display:flex;align-items:baseline;gap:9px;min-width:0;font-size:11.5px;color:var(--dim)}}
.n1 em{{font-style:normal;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.n1 .note{{flex:0 0 auto}}
.step.heavy .n2{{color:var(--think)}}
.note{{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--human);
  border:1px solid color-mix(in srgb,var(--human) 35%,transparent);
  padding:1px 6px;border-radius:2px}}
.note.heavy-note{{color:var(--think);
  border-color:color-mix(in srgb,var(--think) 35%,transparent)}}
.step.think summary{{grid-template-columns:62px minmax(0,1fr) 62px;color:var(--think)}}
.step.think .kind{{margin:0;color:var(--think)}}
.step.think .tsummary{{font-family:var(--sans);font-size:13px;color:var(--dim);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.step.say{{margin:14px 0}}
.bubble{{padding:2px 8px}}
.step.say .lane:before{{background:linear-gradient(var(--rule),transparent)}}

.pane{{margin:2px 8px 14px;padding:14px 16px;background:var(--panel);
  border:1px solid var(--rule);border-radius:3px}}
.pane.split{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.pane h5{{margin:0 0 8px;font:500 10px/1 var(--mono);letter-spacing:.2em;
  text-transform:uppercase;color:var(--dim)}}
.pane pre{{margin:0;font-size:11.5px;line-height:1.55;color:var(--dim);white-space:pre-wrap;
  word-break:break-word;max-height:280px;overflow:auto}}
.step.fail .pane{{border-color:color-mix(in srgb,var(--fail) 30%,var(--rule))}}
.foot{{margin-top:40px;font-size:11px;color:var(--dimmer);line-height:1.8}}
.hidden{{display:none !important}}
@media(max-width:820px){{
  .pane.split{{grid-template-columns:1fr}}
  .step summary{{grid-template-columns:14px 58px minmax(0,1fr) 54px}}
  .n1,.n3{{display:none}}
  .ribbon.turn-ribbon{{width:100%;margin:8px 0 0}}
}}
@media(prefers-reduced-motion:reduce){{*{{transition:none !important}}}}
</style></head>
<body><div class="wrap">

<header class="mast">
  <div class="eyebrow">Session narrative</div>
  <h1>{e(d["title"])}</h1>
  <div class="subline">
    <span><b>{e(d["session_id"][:8])}</b></span><span>{e(d["model"])}</span>
    <span>{e(Path(d["project"]).name)}</span><span>{e(d["branch"])}</span>
    <span>{len(real)} human turns</span>
  </div>

  <div class="strip">{"".join(seg)}</div>
  <div class="striplab"><span>Session start</span>
    <span>bar height = result size · red = failed · amber = retried · violet = heavy</span>
    <span>End</span></div>

  <div class="tiles">
    <div class="tile"><div class="k">Tool calls</div><div class="v">{len(tl_all)}</div></div>
    <div class="tile {"warn" if nf else ""}"><div class="k">Failed</div>
      <div class="v">{nf}</div></div>
    <div class="tile"><div class="k">Retried</div><div class="v">{nr}</div></div>
    <div class="tile accent"><div class="k">Heavy results</div>
      <div class="v">{n_heavy}</div><div class="sub">&ge; {tok(heavy)} tokens</div></div>
  </div>

  <div class="tiles money">
    <div class="tile"><div class="k">Input</div><div class="v">{tok(tk["in"])}</div>
      <div class="sub">uncached residual only</div></div>
    <div class="tile"><div class="k">Cache write</div>
      <div class="v">{tok(tk["cache_write"])}</div>
      <div class="sub">your prompts land here &middot; 1.25&times;</div></div>
    <div class="tile"><div class="k">Cache read</div>
      <div class="v">{tok(tk["cache_read"])}</div><div class="sub">0.1&times; input rate</div></div>
    <div class="tile"><div class="k">Output</div><div class="v">{tok(tk["out"])}</div>
      <div class="sub">generated tokens</div></div>
    <div class="tile accent"><div class="k">Est. cost</div>
      <div class="v">${d["cost"]:.2f}</div><div class="sub">estimate — see /cost</div></div>
  </div>

  <div class="filters" role="group" aria-label="Filter steps">
    <button class="chip" data-f="all" aria-pressed="true">Everything</button>
    <button class="chip" data-f="tools" aria-pressed="false">Tool calls only</button>
    <button class="chip" data-f="trouble" aria-pressed="false">Failures &amp; retries</button>
    <button class="chip" data-f="heavy" aria-pressed="false">Heavy results</button>
  </div>
  <div class="legend">Ledger columns: tool · what it was called with · what came
    back · result size (estimated tokens) · elapsed</div>
</header>

<main>{turns}</main>

<div class="foot">
  Token counts are the transcript's own usage records, counted once per API
  response — one response is written as several entries (thinking, text,
  tool_use) that each repeat the same usage block.
  Under prompt caching, <code>input_tokens</code> is only the residual after the
  last cache breakpoint, so it reads as single digits; a new prompt is billed as
  a cache <em>write</em>. Result sizes are estimated from returned bytes at ~4
  chars/token. Cost is an estimate; <code>/cost</code> and the Usage &amp; Cost
  API are authoritative.<br>
  Retries are inferred, not recorded: a failed call followed by the same tool on
  the same target within the same turn.
</div>
</div>
<script>
const chips=[...document.querySelectorAll('.chip')];
chips.forEach(c=>c.addEventListener('click',()=>{{
  chips.forEach(x=>x.setAttribute('aria-pressed',String(x===c)));
  const f=c.dataset.f;
  document.querySelectorAll('.step').forEach(s=>{{
    const isTool=s.classList.contains('tool');
    const bad=s.dataset.state==='fail'||s.dataset.state==='retry';
    const hv=s.dataset.heavy==='1';
    let show=true;
    if(f==='tools') show=isTool;
    else if(f==='trouble') show=isTool&&bad;
    else if(f==='heavy') show=isTool&&hv;
    s.classList.toggle('hidden',!show);
  }});
  document.querySelectorAll('.turn').forEach(t=>{{
    if(t.classList.contains('aside')){{t.classList.toggle('hidden',f!=='all');return;}}
    const any=[...t.querySelectorAll('.step')].some(s=>!s.classList.contains('hidden'));
    t.classList.toggle('hidden',f!=='all'&&!any);
  }});
}}));
</script>
</body></html>'''


# ===========================================================================
def main():
    ap = argparse.ArgumentParser(
        description="Render a Claude Code session as a readable narrative")
    ap.add_argument("session_id", nargs="?",
                    help="Session ID, or path to a transcript .jsonl")
    ap.add_argument("--latest", action="store_true", help="Most recent session")
    ap.add_argument("--project", help="Project path to search in")
    ap.add_argument("--text", action="store_true",
                    help="Terminal rendering instead of HTML")
    ap.add_argument("--out-file", help="Write to this file")
    ap.add_argument("--no-open", action="store_true",
                    help="Do not open the HTML in a browser")
    args = ap.parse_args()

    try:
        main_file, _ = find_session_files(
            session_id=args.session_id, project_path=args.project,
            latest=args.latest or not args.session_id)
    except FileNotFoundError as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)

    data = build(main_file)
    heavy = calibrate_heavy(data)

    if args.text:
        out = render_text(data, heavy)
        if args.out_file:
            Path(args.out_file).write_text(out)
            print(f"Written to {args.out_file}", file=sys.stderr)
        else:
            print(out)
        return

    out = render_html(data, heavy)
    if args.out_file:
        path = Path(args.out_file)
        path.write_text(out)
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                         prefix=f"narrative-{data['session_id'][:8]}-") as f:
            f.write(out)
            path = Path(f.name)
    print(f"{path}", file=sys.stderr)
    # Self-contained page: opens straight from disk, no web server involved.
    if not args.no_open:
        subprocess.run(["open", str(path)], check=False)


if __name__ == "__main__":
    main()
