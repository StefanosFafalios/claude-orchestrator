#!/usr/bin/env python3
"""UserPromptSubmit hook: inject the compact orchestration policy.

Reads local files only; never calls ``claude``, ``codex`` or the network.
Never fails the prompt: any problem degrades to a short notice in the context.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from classify_task import classify  # noqa: E402
from engine_state import (  # noqa: E402
    PLUGIN_ROOT,
    ConfigError,
    config_path,
    effective_status,
    engine_enabled,
    load_config,
    load_synced_state,
)
from route_engine import route  # noqa: E402

PLUGIN = "cost-aware-orchestrator"
SYSTEM_PROMPT_PREFIXES = ("<agent-message", "<task-notification")
QUESTION_START = re.compile(r"^(?:what|how|why|when|where|which|who|is|are|does|do|can|could|should|would|will|explain)\b")
CHANGE_REQUEST = re.compile(r"\b(?:add|implement|fix|change|create|update|refactor|remove|delete|write|build|make|rename|migrate|deploy|edit|modify|replace|move|rewrite)\b")

POLICY = """ORCHESTRATION POLICY ACTIVE

If an engine returns exit code 42, mark it limited and hand off only remaining work.
If both engines are constrained, avoid fan-out and require human approval."""

DISABLED = "ORCHESTRATION POLICY DISABLED"


def build_context(root: Path, prompt: str = "", session_id: str = "", permission_mode: str = "") -> str:
    try:
        config = load_config(root)
    except ConfigError as exc:
        return f"ORCHESTRATION CONFIG INVALID: {exc}\nDo not orchestrate until the config is fixed."
    if config.get("automatic") is False:
        return DISABLED

    state = load_synced_state(config, root)
    engines = ", ".join(
        f"{name}={effective_status(state[name], config) if config['engines'].get(name, True) else 'disabled'}"
        f"({state[name].get('remaining_pct', 100):g}%)"
        for name in ("claude", "codex") + (("openrouter",) if engine_enabled(config, "openrouter") else ())
    )
    lines = [POLICY, "", f"Engine state: {engines}", f"Plugin scripts: {PLUGIN_ROOT / 'scripts'}"]
    if config["legacy_keys"]:
        lines.append(f"Legacy config keys ignored: {', '.join(config['legacy_keys'])}; "
                     'models now come from "models"/"routing" (see /cost-aware-orchestrator:setup).')
    if not config_path(root).is_file():
        lines.append(
            'No project config: before orchestrating, ask once "Create .claude/orchestrator.config.json '
            'with default orchestration settings?" and copy the plugin template only if the user agrees '
            '(or suggest /cost-aware-orchestrator:setup for the full guided setup).'
        )
    if prompt.strip():
        lines += ["", directive(prompt, config, state, session_id, permission_mode)]
    return "\n".join(lines)


def is_question(prompt: str) -> bool:
    prompt = " ".join(prompt.lower().split())
    return bool((prompt.endswith("?") or QUESTION_START.match(prompt)) and not CHANGE_REQUEST.search(prompt))


LABELS = {"codex": "Codex", "openrouter": "OpenRouter"}


def steps_for(tier: str, config: dict, state: dict, task_key: str, permission_mode: str = ""):
    """Build a compact row from the routing decisions: ``(row, engines used, implementation engine)``."""
    if tier == "trivial":
        return "trivial: handle directly, lowest effort, no review.", set(), None
    impl = route("implementation", tier, config, state, task_key=task_key)
    if impl["engine"] == "none":
        return f"{tier}: No engine available ({impl['reason']}); do not start; ask user.", set(), None
    review = route("review", tier, config, state, task_key=task_key, implementation_model=impl["model"],
                   permission_mode=permission_mode)
    style = route("style_review", tier, config, state)

    def choice(role: str, result: dict) -> str:
        if result["engine"] == "none":
            return "none"
        engine = result["engine"]
        if engine == "claude":
            name = role
        elif role == "codex-reviewer":
            name = "codex-reviewer" if engine == "codex" else f"codex-reviewer[{LABELS[engine]}]"
        else:
            name = LABELS[engine]
        effort = "ASK" if result["effort"] == "ask" else result["effort"]
        return f"{name}/{result['model']}/{effort}"

    steps = []
    results = [impl, review, style]
    if tier in ("complex", "critical", "supercritical"):
        scout = route("scout", tier, config, state)
        arch = route("architecture", tier, config, state, task_key=task_key, permission_mode=permission_mode)
        results += [scout, arch]
        steps += [f"scout={choice('scout', scout)}",
                  f"architect {'if unclear' if tier == 'complex' else 'required'}={choice('architect', arch)}"]
    marker = f"[{LABELS[impl['engine']]}] " if impl["engine"] in LABELS else ""
    steps.append(f"implement={marker}{choice('implementer', impl)}")
    review_role = "verifier" if review["engine"] == "claude" else "codex-reviewer"
    steps.append(f"review={choice(review_role, review)}")
    steps.append(f"style={choice('style-reviewer', style)}" if style["engine"] != "none"
                 else "review covers style")
    if impl["requires_human_review"]:
        steps.append("quota constrained: one atomic task, then human review")
    return f"{tier}: " + "; ".join(steps) + ".", {r["engine"] for r in results}, impl["engine"]


def directive(prompt: str, config: dict, state: dict, session_id: str = "", permission_mode: str = "") -> str:
    """Supply per-tier routes for the model to classify with conversation context."""
    if is_question(prompt):
        return "THIS PROMPT: question. Answer it directly: no subagents, no Codex, no file edits."
    cls = classify(prompt)
    task_key = hashlib.sha256((prompt + "\0" + session_id).encode("utf-8")).hexdigest()[:16]
    lines = [
        "Classify this prompt using the whole conversation, state `route=<tier>: <reason>` in one line, then follow that tier's row. A short approval (yes, go ahead) inherits the tier of what it approves, never lower.",
        "Rubric: trivial = conversational, git/commit/push, one-line or config edits; standard = a well-defined change in a few files; complex = multi-component, unclear root cause, design work; critical = security, auth, data/schema, payments, production, irreversible actions; supercritical = needs frontier reasoning beyond known solutions (top model reasons, plans and reviews; never implements).",
    ]
    if cls["route"] in ("complex", "critical"):
        lines.append(f"Floor: at least {cls['route']} ({cls['reason']}); never pick a lower tier.")
    tiers = ("trivial", "standard", "complex", "critical", "supercritical")
    floor = tiers.index(cls["route"]) if cls["route"] in ("complex", "critical") else 0
    built = [steps_for(tier, config, state, task_key, permission_mode) for tier in tiers[floor:]]
    rows = [row for row, _, _ in built]
    used = set().union(*(engines for _, engines, _ in built))
    delegated = {impl for _, _, impl in built}
    lines.append(f"Agents are {PLUGIN}:<name>.")
    lines.append(f"Test: {config['commands'].get('test') or 'none configured'}.")
    if "codex" in used:
        lines.append("Codex: write_handoff.py, then CODEX_SANDBOX=workspace-write CODEX_MODEL=<model> CODEX_EFFORT=<effort> codex_with_fallback.sh <handoff>.")
    if "openrouter" in used:
        lines.append("OpenRouter: write_handoff.py, then OPENROUTER_MODEL=<model> OPENROUTER_EFFORT=<effort> OPENROUTER_SANDBOX=workspace-write openrouter_run.py <handoff>; exit 43 = paused over budget: tell the user and ask continue (--resume <run>) or stop (--stop <run>).")
    for engine, label in LABELS.items():
        if engine in delegated:
            lines.append(f"Rows marked [{label}] implement on {label}: delegate, do not edit files in this Claude session.")
    if any("/ASK" in row for row in rows):
        lines.append(f"Supercritical task id {task_key}: before the first top-model step ask the user which effort (low|medium|high|xhigh|max; cost rises steeply), then run engine_state.py set-effort --task-id {task_key} --effort <e>. Route its sub-tasks with route_engine.py --task-id <sub> --parent-task-id {task_key} --permission-mode {permission_mode or 'default'}.")
    lines.extend(rows)
    lines.append("If the tier changes mid-task, switch to that row, carry finished work over via a handoff, never downgrade critical.")
    lines.append("For multi-task plans, route each task separately with route_engine.py --task-id <task>.")
    lines.append("Visibility: one line before each step saying what starts and why. Run Codex via Bash run_in_background and wait for the completion notification without reading output early. Relay approach and key decisions in at most 3 bullets.")
    return "\n".join(lines)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    prompt = str(payload.get("prompt") or "")
    if prompt.lstrip().startswith(SYSTEM_PROMPT_PREFIXES):
        return 0  # Subagent hand-backs and task notifications are not new user tasks.
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd())
    context = build_context(root, prompt, payload.get("session_id") or "", str(payload.get("permission_mode") or ""))
    output = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
