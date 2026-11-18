#!/usr/bin/env python3
"""Cheap tier decider: one OpenRouter call classifies a prompt by the hook rubric.

Usage: decide_route.py [--prompt-file F]  (default: the prompt on stdin)
Exit 0 prints ``{"route", "reason", "model"}``; the route never falls below the keyword
safety floor. Exit 3 means "fallback: the session model classifies": OpenRouter is off,
has no key, is constrained or over budget, has no decider row, or the call failed in any
way. A failure here never marks engine state: the decider is advisory and never blocks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

import openrouter_budget as ob
from classify_task import FLOOR_ROUTES, RUBRIC, classify
from engine_state import effective_status, engine_enabled, load_config, load_synced_state, redact
from model_map import ROUTES
from route_engine import route

TIMEOUT = 8
MAX_PROMPT_CHARS = 8000
INSTRUCTIONS = (f"Classify the coding task in the user message into one tier. {RUBRIC} "
                'Reply with JSON only: {"route": "<tier>", "reason": "<at most 20 words>"}.')


class Fallback(Exception):
    """The decider cannot answer; the session model classifies instead."""


def decider_route(config: dict, state: dict) -> dict:
    """The routed decider model on OpenRouter; raises ``Fallback`` when it cannot run."""
    if not engine_enabled(config, "openrouter"):
        raise Fallback("OpenRouter is not set up")
    status = effective_status(state["openrouter"], config)
    if status != "healthy":
        raise Fallback(f"OpenRouter is {status}")
    # Cached snapshot only: no /key call on this hot path. cap_spent is window-aware and
    # covers the daily/weekly soft caps too, so a stale snapshot never blocks forever.
    snapshot = ob.read_snapshot()
    if snapshot and ob.cap_spent(snapshot, config["openrouter"]["budget"], time.time()):
        raise Fallback("OpenRouter budget is spent")
    if "decider" not in config["routing"]:
        raise Fallback("the config has no routing.decider row")
    picked = route("decider", "standard", config, state, exclude=("claude", "codex"))
    if picked["engine"] != "openrouter":
        raise Fallback("no decider model is available on OpenRouter")
    return picked


def usable(config: dict, state: dict) -> bool:
    try:
        decider_route(config, state)
    except Fallback:
        return False
    return True


def decide(prompt: str, config: dict, state: dict) -> dict:
    picked = decider_route(config, state)
    body = {
        "model": picked["model"],
        "messages": [{"role": "system", "content": INSTRUCTIONS},
                     {"role": "user", "content": redact(prompt)[:MAX_PROMPT_CHARS]}],
        "max_tokens": 300,
        "temperature": 0,
        "reasoning": {"effort": picked["effort"]},
    }
    key = os.environ[config["openrouter"]["key_env"]]
    request = urllib.request.Request(ob.api_base() + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        content = json.loads(response.read().decode("utf-8"))["choices"][0]["message"]["content"]
    answer = json.loads(content[content.index("{"):content.rindex("}") + 1])
    decided = answer.get("route")
    if decided not in ROUTES:
        raise Fallback(f"invalid route {decided!r}")
    reason = f"decider ({picked['model']}): {str(answer.get('reason', ''))[:200]}"
    floor = classify(prompt)  # the raw prompt: redaction would hide credential keywords
    if floor["route"] in FLOOR_ROUTES and ROUTES.index(decided) < ROUTES.index(floor["route"]):
        decided, reason = floor["route"], f"keyword floor over decider's {decided}: {floor['reason']}"
    return {"route": decided, "reason": reason, "model": picked["model"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prompt-file", help="file containing the prompt (default: stdin)")
    try:
        args = parser.parse_args(argv)
        if args.prompt_file:
            with open(args.prompt_file, encoding="utf-8") as handle:
                prompt = handle.read()
        else:
            prompt = sys.stdin.read()
        config = load_config()
        result = decide(prompt, config, load_synced_state(config))
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - advisory: every failure (even a bad flag) hands classification back
        print(f"fallback: session model classifies ({redact(str(exc))[:300]})", file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
