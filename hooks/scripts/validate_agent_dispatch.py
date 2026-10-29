#!/usr/bin/env python3
"""PreToolUse guardrail for subagent dispatches (Agent/Task tool).

Denies dispatches of this plugin's agents that break the routing policy. Compliant
or unrelated dispatches produce no output, so Claude Code's normal permission flow
applies unchanged (emitting "allow" would bypass the user's permission rules).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from classify_task import classify  # noqa: E402
from engine_state import ConfigError, load_config, load_state  # noqa: E402

PLUGIN = "cost-aware-orchestrator"
AGENTS = {"scout", "implementer", "architect", "verifier", "codex-reviewer"}
JUSTIFIED = re.compile(r"(?im)^\s*justification\s*:|\bescalat(e|ed|ing|ion)\b")


def plugin_agent(subagent_type: str) -> str | None:
    prefix, _, name = subagent_type.rpartition(":")
    if name in AGENTS and prefix in ("", PLUGIN):
        return name
    return None


def violation(agent: str, prompt: str, root: Path) -> str | None:
    try:
        config = load_config(root)
    except ConfigError:
        return None  # The scripts report config errors; never block on them here.
    if config.get("automatic") is False:
        return None
    state = load_state(root)

    if agent == "implementer" and state["claude"].get("status") == "limited":
        return "Claude is marked limited: write a handoff and route implementation to Codex instead."
    if agent == "codex-reviewer" and state["codex"].get("status") == "limited":
        return "Codex is marked limited: use the verifier agent for review instead."
    if agent == "architect":
        route = classify(prompt)["route"]
        if route == "trivial":
            return "Trivial task sent to architect (opus/high): handle it directly or use scout/implementer."
        if route == "standard" and not JUSTIFIED.search(prompt):
            return ("Standard task sent to architect without justification: use implementer, or add a "
                    "'Justification:' line explaining why deep analysis is needed.")
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool_input = payload.get("tool_input") or {}
    agent = plugin_agent(str(tool_input.get("subagent_type") or ""))
    if not agent:
        return 0
    # The prompt is the task; the model-written description only adds keyword noise.
    prompt = str(tool_input.get("prompt") or tool_input.get("description") or "")
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd())
    reason = violation(agent, prompt, root)
    if reason:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"[{PLUGIN}] {reason}",
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
