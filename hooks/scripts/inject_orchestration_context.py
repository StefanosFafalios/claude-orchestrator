#!/usr/bin/env python3
"""UserPromptSubmit hook: inject the compact orchestration policy.

Reads local files only; never calls ``claude``, ``codex`` or the network.
Never fails the prompt: any problem degrades to a short notice in the context.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from classify_task import classify  # noqa: E402
from engine_state import (  # noqa: E402
    PLUGIN_ROOT,
    ConfigError,
    config_path,
    effective_status,
    load_config,
    load_state,
)
from route_engine import route  # noqa: E402

PLUGIN = "cost-aware-orchestrator"
SYSTEM_PROMPT_PREFIXES = ("<agent-message", "<task-notification")

POLICY = """ORCHESTRATION POLICY ACTIVE

Before nontrivial work:
1. Classify the task as trivial, standard, complex, or critical.
2. Check Claude and Codex quota state.
3. Route implementation and review using scripts/route_engine.py.
4. Use the lowest sufficient Claude model and effort.
5. Prefer Codex for independent review when both engines are healthy.
6. If an engine returns exit code 42, mark it limited and hand off only remaining work.
7. If both engines are constrained, avoid fan-out and require human approval.

Do not run the full workflow for trivial conversational requests."""

DISABLED = "ORCHESTRATION POLICY DISABLED"


def build_context(root: Path, prompt: str = "") -> str:
    try:
        config = load_config(root)
    except ConfigError as exc:
        return f"ORCHESTRATION CONFIG INVALID: {exc}\nDo not orchestrate until the config is fixed."
    if config.get("automatic") is False:
        return DISABLED

    state = load_state(root)
    engines = ", ".join(
        f"{name}={effective_status(state[name], config) if config['engines'].get(name, True) else 'disabled'}"
        f"({state[name].get('remaining_pct', 100):g}%)"
        for name in ("claude", "codex")
    )
    lines = [POLICY, "", f"Engine state: {engines}", f"Plugin scripts: {PLUGIN_ROOT / 'scripts'}"]
    if not config_path(root).is_file():
        lines.append(
            'No project config: before orchestrating, ask once "Create .claude/orchestrator.config.json '
            'with default orchestration settings?" and copy the plugin template only if the user agrees.'
        )
    if prompt.strip():
        lines += ["", directive(prompt, config, state)]
    return "\n".join(lines)


def _agent(name: str, choice: dict) -> str:
    return f"the `{PLUGIN}:{name}` agent (model {choice['model']}, effort {choice['effort']})"


def directive(prompt: str, config: dict, state: dict) -> str:
    """Concrete routing for this prompt, so the model does not have to decide to orchestrate."""
    cls = classify(prompt)
    task_route = cls["route"]
    impl = route("implementation", task_route, config, state)
    review = route("review", task_route, config, state, cls["requires_codex_review"])
    head = f"THIS PROMPT: route={task_route} ({cls['reason']}). If it is only a question, just answer it."
    if impl["engine"] == "none":
        return f"{head}\nNo engine available ({impl['reason']}): do not start work; ask the user how to proceed."
    if task_route == "trivial":
        return f"{head}\nHandle it directly at the lowest effort: no subagents, no review fan-out."

    steps = []
    if task_route in ("complex", "critical"):
        scout = route("scout", task_route, config, state)
        if scout["engine"] == "claude":
            steps.append(f"scout with {_agent('scout', scout)}")
        arch = route("architecture", task_route, config, state)
        if arch["engine"] == "claude":
            optional = " only if the root cause or design is unclear" if task_route == "complex" else ""
            steps.append(f"plan with {_agent('architect', arch)}{optional}")
    if impl["engine"] == "claude":
        steps.append(f"implement with {_agent('implementer', impl)}")
    else:
        steps.append(f"implement on Codex ({impl['reason']}): write a handoff with write_handoff.py, then run "
                     f"CODEX_SANDBOX=workspace-write CODEX_MODEL={impl['model']} codex_with_fallback.sh <handoff>; "
                     f"on exit 42 run engine_state.py mark-limit --engine codex --message \"<first line>\"")
    steps.append(f"run the project's configured test command: {config['commands'].get('test') or '(none configured)'}")
    if review["engine"] == "claude":
        steps.append(f"review with {_agent('verifier', review)}")
    elif review["engine"] == "codex":
        steps.append(f"review independently with the `{PLUGIN}:codex-reviewer` agent ({review['reason']}); "
                     f"put the scripts directory {PLUGIN_ROOT / 'scripts'} and the task id in its prompt")
    numbered = "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
    human = "\nQuota is constrained: one atomic task only, then ask for human review." if impl["requires_human_review"] else ""
    if impl["engine"] == "codex":
        rule = ("REQUIRED (quota routing, do not override for small changes): do not edit files in this Claude "
                "session; every edit spends the constrained Claude quota.")
    else:
        rule = (f"Orchestrate it (pass the model to each Agent call). If this session already runs on "
                f"{impl['model']} or a cheaper model, you may make a small change inline, but still run tests and review.")
    return f"{head}\n{rule}\n{numbered}{human}"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    prompt = str(payload.get("prompt") or "")
    if prompt.lstrip().startswith(SYSTEM_PROMPT_PREFIXES):
        return 0  # Subagent hand-backs and task notifications are not new user tasks.
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd())
    context = build_context(root, prompt)
    output = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
