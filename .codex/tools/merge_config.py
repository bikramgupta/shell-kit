#!/usr/bin/env python3
"""Merge shell-kit's managed Codex config without clobbering user/app state.

Only top-level model defaults and the [otel] table are owned by shell-kit.
Everything else in ~/.codex/config.toml (projects, plugins, MCP servers,
desktop preferences, notifications, permissions) is preserved byte-for-byte.
"""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path


MANAGED_TOP_LEVEL = ("model", "model_reasoning_effort")
MANAGED_TABLES = ("otel",)
TABLE_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")


def table_root(line: str):
    match = TABLE_RE.match(line)
    if not match:
        return None
    name = match.group(1).strip()
    return name.split(".", 1)[0].strip('"')


def extract_top_level(source: str):
    values = {}
    in_table = False
    for line in source.splitlines():
        if table_root(line) is not None:
            in_table = True
        if in_table:
            continue
        for key in MANAGED_TOP_LEVEL:
            if re.match(rf"^\s*{re.escape(key)}\s*=", line):
                values[key] = line.strip()
    missing = [key for key in MANAGED_TOP_LEVEL if key not in values]
    if missing:
        raise ValueError("managed source is missing: " + ", ".join(missing))
    return values


def extract_table(source: str, root: str):
    lines = source.splitlines()
    selected = []
    copying = False
    for line in lines:
        found = table_root(line)
        if found is not None:
            copying = found == root
        if copying:
            selected.append(line)
    if not selected:
        raise ValueError(f"managed source is missing [{root}]")
    while selected and not selected[-1].strip():
        selected.pop()
    return "\n".join(selected)


def strip_managed(target: str):
    output = []
    skipping_table = False
    in_any_table = False
    for line in target.splitlines():
        found = table_root(line)
        if found is not None:
            in_any_table = True
            skipping_table = found in MANAGED_TABLES
            if skipping_table:
                continue
        if skipping_table:
            continue
        if not in_any_table and any(
            re.match(rf"^\s*{re.escape(key)}\s*=", line)
            for key in MANAGED_TOP_LEVEL
        ):
            continue
        output.append(line)
    while output and not output[-1].strip():
        output.pop()
    return "\n".join(output)


def merge(source: str, target: str):
    top_level = extract_top_level(source)
    body = strip_managed(target)
    tables = [extract_table(source, root) for root in MANAGED_TABLES]
    pieces = [
        "# Managed by shell-kit: model defaults + local OpenTelemetry",
        *(top_level[key] for key in MANAGED_TOP_LEVEL),
    ]
    if body:
        pieces.extend(["", body])
    pieces.extend(["", "# Managed by shell-kit", *tables, ""])
    return "\n".join(pieces)


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent), delete=False
    )
    try:
        with handle:
            handle.write(content)
        os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    finally:
        if os.path.exists(handle.name):
            os.unlink(handle.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--stdout", action="store_true", help="print merged config without writing")
    args = parser.parse_args()
    source = args.source.expanduser().read_text(encoding="utf-8")
    target_path = args.target.expanduser()
    target = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
    result = merge(source, target)
    if args.stdout:
        print(result, end="")
    else:
        atomic_write(target_path, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
