#!/usr/bin/env python3
"""PreToolUse guardrail for subagent dispatches (Agent/Task tool).

Denies any dispatch naming a model the Claude subscription cannot run (it would silently
substitute another model; this holds even with ``automatic: false``), and dispatches of this
plugin's agents that break the routing policy. Compliant
or unrelated dispatches produce no output, so Claude Code's normal permission flow
applies unchanged (emitting "allow" would bypass the user's permission rules).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import model_map  # noqa: E402
from engine_state import ENGINES, ConfigError, load_config, load_state  # noqa: E402

PLUGIN = "cost-aware-orchestrator"
LABELS = {"codex": "Codex", "openrouter": "OpenRouter"}
AGENTS = {"scout", "implementer", "architect", "verifier", "codex-reviewer", "style-reviewer"}


def plugin_agent(subagent_type: str) -> str | None:
    prefix, _, name = subagent_type.rpartition(":")
    if name in AGENTS and prefix in ("", PLUGIN):
        return name
    return None


def unserved_model(model: str, config: dict) -> str | None:
    """A catalog model the Claude subscription cannot run: Claude would silently substitute another."""
    model_id = next((m for m in (model_map.canonical(config, e, model) for e in ENGINES) if m), None) if model else None
    if model_id and not model_map.engine_name(config, model_id, "claude"):
        return (f"{model} is not served by the Claude subscription (it would silently run another model): "
                "use the routed model, or run it through OpenRouter with openrouter_run.py.")
    return None


def violation(agent: str, prompt: str, config: dict, root: Path) -> str | None:
    if config.get("automatic") is False:
        return None
    state = load_state(root)

    blocked = ("limited", "unavailable")
    if agent == "implementer" and state["claude"].get("status") in blocked:
        return f"Claude is marked {state['claude']['status']}: write a handoff and route implementation to another engine instead."
    if agent == "codex-reviewer":
        engine = "openrouter" if "openrouter_run.py" in prompt or "review_run.py" in prompt else "codex"
        if state[engine].get("status") in blocked:
            return f"{LABELS[engine]} is marked {state[engine]['status']}: use the verifier agent for review instead."
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool_input = payload.get("tool_input") or {}
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd())
    try:
        config = load_config(root)
    except ConfigError:
        return 0  # The scripts report config errors; never block on them here.
    reason = unserved_model(str(tool_input.get("model") or ""), config)  # any agent type, not just ours
    agent = plugin_agent(str(tool_input.get("subagent_type") or ""))
    if agent and not reason:
        # The prompt is the task; the model-written description only adds keyword noise.
        prompt = str(tool_input.get("prompt") or tool_input.get("description") or "")
        reason = violation(agent, prompt, config, root)
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
