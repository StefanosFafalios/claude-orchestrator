#!/usr/bin/env python3
"""Fetch and filter the OpenRouter model catalog for the review-models skill.

Standalone on purpose (maintainer tool, not shipped with the plugin). Usage:
    catalog.py [--input FILE] [--min-context N]
Reads GET ${OPENROUTER_API_BASE:-https://openrouter.ai/api/v1}/models (public, no key)
or a saved response, and prints the surviving models as JSON, cheapest first.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone


def _per_mtok(value) -> float:
    try:
        return float(value) * 1_000_000
    except (TypeError, ValueError):
        return 0.0


def filter_models(models, min_context: int = 64000, today: str = "") -> list:
    """Keep tool-calling, long-context, paid, non-expired models; sort by blended price."""
    if isinstance(models, dict):
        models = models.get("data", [])
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = []
    for m in models:
        mid = str(m.get("id", ""))
        pricing = m.get("pricing") or {}
        prompt = _per_mtok(pricing.get("prompt"))
        completion = _per_mtok(pricing.get("completion"))
        expires = m.get("expiration_date")
        if (
            "tools" not in (m.get("supported_parameters") or [])
            or (m.get("context_length") or 0) < min_context
            or mid.endswith((":free", ":batch"))
            or mid.startswith("~")
            or prompt <= 0
            # ISO dates compare correctly as strings; a datetime past today's date is still >= it
            or (expires and str(expires)[:10] < today)
        ):
            continue
        out.append({
            "id": mid,
            "name": m.get("name", mid),
            "context_length": m.get("context_length"),
            "prompt_usd_per_mtok": prompt,
            "completion_usd_per_mtok": completion,
            "blended_usd_per_mtok": (3 * prompt + completion) / 4,
        })
    return sorted(out, key=lambda e: e["blended_usd_per_mtok"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", help="saved /models response instead of fetching")
    ap.add_argument("--min-context", type=int, default=64000)
    args = ap.parse_args(argv)
    try:
        if args.input:
            with open(args.input, encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            base = os.environ.get("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
            with urllib.request.urlopen(base.rstrip("/") + "/models", timeout=30) as resp:
                data = json.load(resp)
    except (OSError, ValueError) as exc:
        print("catalog: could not load models: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(filter_models(data, args.min_context), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
