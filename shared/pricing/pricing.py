#!/usr/bin/env python3
"""Model pricing, loaded from data files rather than compiled in.

Both the Claude and Codex analyzers use this. It is the one piece they share,
because a price list is data about the world, not adapter logic: when OpenAI or
Anthropic changes a rate, or when you start using a model slug that has no
public price, that is a file to edit -- not a Python constant to hunt down in
two separate 1000-line parsers.

Sources are MERGED in this order, later overriding earlier per model key:

  1. models.json next to this file      shipped defaults
  2. ~/.config/ai-tools/pricing.json    your overrides; survives deploy.sh
  3. $AI_MODEL_PRICING                  explicit path
  4. --pricing-file FILE                per invocation

An unmatched model is UNPRICED, not guessed. Callers get None and are expected
to say "N/A" and name the model. That is the whole point: a plausible wrong
number is worse than an honest gap, because it is the kind of wrong you never
notice.

Token buckets are normalized to four names; each adapter maps its provider's
fields onto them:

  input        uncached input tokens
  output       output tokens (reasoning included -- it is not billed extra)
  cache_write  tokens written to cache  (Anthropic; 0 elsewhere)
  cache_read   tokens served from cache (Anthropic cache_read, OpenAI cached_input)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

BUCKETS = ("input", "output", "cache_write", "cache_read")

DEFAULT_FILE = Path(__file__).resolve().parent / "models.json"
USER_FILE = Path(
    os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
) / "ai-tools" / "pricing.json"
ENV_VAR = "AI_MODEL_PRICING"

# Suffix characters a bare '*' is allowed to match across. '.' is excluded on
# purpose -- see the wildcard note in models.json.
VARIANT_SEPARATORS = "-[@:_"


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class Pricing:
    """A resolved price table plus the provenance of where it came from."""

    def __init__(self, models, defaults, sources):
        self.models = models
        self.defaults = defaults
        self.sources = sources
        self._cache = {}

    # -- resolution ---------------------------------------------------------
    def _match(self, model):
        """Most specific entry for a model id, or None.

        Exact keys beat wildcards; among wildcards the longest literal prefix
        wins, so 'gpt-5.4-mini*' is chosen over 'gpt-5.4*' for a mini slug.
        """
        model = (model or "").strip()
        if not model:
            return None
        if model in self.models:
            return self.models[model]

        best, best_len = None, -1
        for key, entry in self.models.items():
            if key.endswith("**"):
                stem, loose = key[:-2], True
            elif key.endswith("*"):
                stem, loose = key[:-1], False
            else:
                continue
            if not model.startswith(stem):
                continue
            rest = model[len(stem):]
            if not loose and rest and rest[0] not in VARIANT_SEPARATORS:
                continue
            if len(stem) > best_len:
                best, best_len = entry, len(stem)
        return best

    def rates(self, model):
        """Per-1M-token rates for every bucket, or None if unpriced."""
        if model in self._cache:
            return self._cache[model]

        entry = self._match(model)
        out = None
        if entry:
            try:
                inp = float(entry["input"])
                outp = float(entry["output"])
            except (KeyError, TypeError, ValueError):
                inp = outp = None
            if inp is not None:
                cw = entry.get("cache_write")
                cr = entry.get("cache_read")
                if cr is None:
                    cr = entry.get("cached_input")
                out = {
                    "input": inp,
                    "output": outp,
                    "cache_write": float(cw) if cw is not None
                    else inp * self.defaults["cache_write_multiplier"],
                    "cache_read": float(cr) if cr is not None
                    else inp * self.defaults["cache_read_multiplier"],
                }
        self._cache[model] = out
        return out

    def known(self, model):
        return self.rates(model) is not None

    # -- costing ------------------------------------------------------------
    def cost(self, model, usage):
        """USD for one model's usage, or None when the model has no price.

        `usage` is a mapping over BUCKETS; missing buckets count as zero.
        """
        r = self.rates(model)
        if r is None:
            return None
        return sum(float(usage.get(k) or 0) * r[k] for k in BUCKETS) / 1e6

    def cost_many(self, per_model):
        """Cost across {model: usage}. Returns (total, sorted unpriced models).

        `total` is the cost of the priced subset -- never None, so callers can
        always render a number, and never silently complete, because any
        unpriced model is named in the second element.
        """
        total, unpriced = 0.0, set()
        for model, usage in (per_model or {}).items():
            c = self.cost(model, usage)
            if c is None:
                if any(float(usage.get(k) or 0) for k in BUCKETS):
                    unpriced.add(model or "unknown")
            else:
                total += c
        return total, sorted(unpriced)

    # -- provenance ---------------------------------------------------------
    def describe(self):
        return " <- ".join(str(s) for s in self.sources) or "(no price file found)"

    def hint(self, models=None):
        """One line telling the user exactly how to fix an N/A."""
        who = ", ".join(models) if models else "this model"
        return (f"No price configured for: {who}. Add rates to {USER_FILE} "
                f"(see {DEFAULT_FILE} for the format).")


def load(extra=None):
    """Merge every available source into one Pricing table."""
    models, defaults, sources = {}, {}, []
    candidates = [DEFAULT_FILE, USER_FILE]
    env = os.environ.get(ENV_VAR)
    if env:
        candidates.append(Path(env).expanduser())
    if extra:
        candidates.append(Path(extra).expanduser())

    for path in candidates:
        data = _read(path)
        if not data:
            continue
        sources.append(path)
        d = data.get("defaults")
        if isinstance(d, dict):
            defaults.update(d)
        m = data.get("models")
        if isinstance(m, dict):
            for key, entry in m.items():
                if isinstance(entry, dict):
                    models[key] = entry

    defaults.setdefault("cache_write_multiplier", 1.25)
    defaults.setdefault("cache_read_multiplier", 0.1)
    return Pricing(models, defaults, sources)


def bootstrap():
    """Import path for a deployed copy sitting next to the caller's script.

    deploy.sh copies this file and models.json into each tool directory, so an
    installed analyzer finds them as siblings; in the repo checkout they live in
    shared/pricing/. Adapters call `sys.path` insertion for both before import.
    """
    return [Path(__file__).resolve().parent]


if __name__ == "__main__":
    import sys

    p = load(sys.argv[2] if len(sys.argv) > 2 else None)
    if len(sys.argv) > 1:
        model = sys.argv[1]
        r = p.rates(model)
        print(f"{model}: {r if r else 'UNPRICED -- ' + p.hint([model])}")
    else:
        print(f"sources: {p.describe()}")
        for key in sorted(p.models):
            r = p.rates(key.rstrip("*")) or p.rates(key)
            print(f"  {key:<26} in {r['input']:>7.3f}  out {r['output']:>7.3f}  "
                  f"cw {r['cache_write']:>7.3f}  cr {r['cache_read']:>7.3f}")
