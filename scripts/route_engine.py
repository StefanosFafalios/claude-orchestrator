#!/usr/bin/env python3
"""Choose engine, model and effort for one orchestration role.

Reads the merged configuration and ``.claude/engine-state.json`` and prints JSON:
``{"engine", "model", "effort", "reason", "requires_human_review", "scope"}``.
``engine`` is ``claude``, ``codex`` or ``none``.
"""

from __future__ import annotations

import argparse
import json
import sys

from engine_state import ConfigError, effective_status, load_config, load_synced_state

ROLES = ("implementation", "review", "scout", "architecture")
ROUTES = ("trivial", "standard", "complex", "critical")
CODEX_EFFORT = {"trivial": "low", "standard": "medium", "complex": "high", "critical": "high"}


class Engines:
    """Per-engine availability derived from config and state."""

    def __init__(self, config: dict, state: dict):
        self.config = config
        self.state = state
        self.status = {e: effective_status(state[e], config) for e in ("claude", "codex")}
        enabled = config.get("engines", {})
        self.enabled = {e: bool(enabled.get(e, True)) for e in ("claude", "codex")}

    def usable(self, engine: str) -> bool:
        return self.enabled[engine] and self.status[engine] != "limited"

    def constrained(self, engine: str) -> bool:
        return self.status[engine] != "healthy"

    def healthy(self, engine: str) -> bool:
        return self.enabled[engine] and self.status[engine] == "healthy"

    def remaining(self, engine: str) -> float:
        return float(self.state[engine].get("remaining_pct", 0))

    def fallback_allowed(self, src: str, dst: str) -> bool:
        return bool(self.config.get("fallback", {}).get(f"{src}_to_{dst}", True))


def _claude(config: dict, key: str, reason: str) -> dict:
    defaults = config["defaults"]
    return {"engine": "claude", "model": defaults[f"{key}_model"], "effort": defaults[f"{key}_effort"], "reason": reason}


def _claude_tier(model: str, effort: str, reason: str) -> dict:
    return {"engine": "claude", "model": model, "effort": effort, "reason": reason}


def _codex(config: dict, route: str, reason: str) -> dict:
    return {
        "engine": "codex",
        "model": config.get("codex", {}).get("default_model", ""),
        "effort": CODEX_EFFORT[route],
        "reason": reason,
    }


def _none(reason: str) -> dict:
    return {"engine": "none", "model": "", "effort": "", "reason": reason}


def _quota_route(engines: Engines, route: str, healthy_choice: dict, claude_choice: dict) -> dict:
    """Shared quota logic for implementation and review.

    ``healthy_choice`` applies when both engines are healthy; ``claude_choice`` is the
    Claude tier to use when quota forces Claude.
    """
    config = engines.config
    claude_ok, codex_ok = engines.usable("claude"), engines.usable("codex")
    # Claude is the primary engine; Codex only takes Claude's work when that direction is allowed.
    if engines.constrained("claude") and not engines.fallback_allowed("claude", "codex"):
        codex_ok = False

    if not claude_ok and not codex_ok:
        return _none("no engine available (limited, disabled or fallback not allowed)")
    if claude_ok and codex_ok:
        c_con, x_con = engines.constrained("claude"), engines.constrained("codex")
        if c_con and x_con:
            higher = "codex" if engines.remaining("codex") > engines.remaining("claude") else "claude"
            reason = f"both engines constrained; {higher} has more remaining quota"
            return _constrained(engines, route, higher, claude_choice, reason)
        if c_con:
            return _codex(config, route, f"claude is {engines.status['claude']}; codex is healthy")
        if x_con:
            return dict(claude_choice, reason=f"codex is {engines.status['codex']}; claude is healthy")
        return healthy_choice
    only = "claude" if claude_ok else "codex"
    other = "codex" if only == "claude" else "claude"
    why = "disabled" if not engines.enabled[other] else engines.status[other]
    reason = f"{other} is {why}; using {only}"
    if engines.constrained(only):
        return _constrained(engines, route, only, claude_choice, reason + f" (itself {engines.status[only]})")
    if only == "codex":
        return _codex(config, route, reason)
    return dict(claude_choice, reason=reason)


def _constrained(engines: Engines, route: str, engine: str, claude_choice: dict, reason: str) -> dict:
    choice = _codex(engines.config, route, reason) if engine == "codex" else dict(claude_choice, reason=reason)
    choice["scope"] = "single_atomic_task"
    choice["requires_human_review"] = bool(
        engines.config.get("fallback", {}).get("require_human_review_when_both_limited", True)
    )
    return choice


def route_implementation(engines: Engines, route: str) -> dict:
    config = engines.config
    tiers = {
        "trivial": _claude(config, "scout", "trivial task: cheapest Claude tier"),
        "standard": _claude(config, "implementation", "standard task: default implementation tier"),
        "complex": _claude_tier(config["defaults"]["implementation_model"], "high",
                                "complex task: implementation model at high effort; escalate to architect only if needed"),
        "critical": _claude(config, "architect", "critical task: strongest Claude tier"),
    }
    return _quota_route(engines, route, tiers[route], tiers[route])


def route_review(engines: Engines, route: str, requires_codex_review: bool) -> dict:
    config = engines.config
    if route == "critical":
        claude_choice = _claude(config, "critical_review", "critical review on Claude")
    elif route == "trivial":
        claude_choice = _claude(config, "scout", "trivial change: light review")
    else:
        claude_choice = _claude(config, "review", "focused Claude review")

    wants_codex = route == "critical" or (route == "complex" and requires_codex_review)
    if wants_codex and engines.healthy("codex"):
        healthy_choice = _codex(config, route, "independent adversarial review on a different engine")
    else:
        healthy_choice = claude_choice
    return _quota_route(engines, route, healthy_choice, claude_choice)


def route_scout(engines: Engines, route: str) -> dict:
    config = engines.config
    if engines.healthy("claude"):
        return _claude(config, "scout", "scouting on the cheapest Claude tier")
    if route in ("complex", "critical") and engines.usable("codex"):
        return _codex(config, "trivial", f"claude is {engines.status['claude']}; {route} task needs exploration")
    return _none("claude constrained and task does not require repository exploration; skip scouting")


def route_architecture(engines: Engines, route: str) -> dict:
    config = engines.config
    if engines.healthy("claude"):
        return _claude(config, "architect", "architecture on the strongest Claude tier")
    if engines.usable("codex") and engines.fallback_allowed("claude", "codex"):
        return _codex(config, "critical", f"claude is {engines.status['claude']}; architecture on codex")
    if engines.usable("claude"):
        model = config["defaults"]["implementation_model"]
        choice = _claude_tier(model, "high", "claude near limit and codex unavailable; downgrade from opus")
        return dict(choice, requires_human_review=True)
    return _none("no engine available for architecture")


def route(role: str, task_route: str, config: dict, state: dict, requires_codex_review: bool = False) -> dict:
    engines = Engines(config, state)
    if role == "implementation":
        result = route_implementation(engines, task_route)
    elif role == "review":
        result = route_review(engines, task_route, requires_codex_review)
    elif role == "scout":
        result = route_scout(engines, task_route)
    else:
        result = route_architecture(engines, task_route)
    result.setdefault("requires_human_review", result["engine"] == "none")
    result.setdefault("scope", "full_task" if result["engine"] != "none" else "none")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--task-route", choices=ROUTES, default="standard")
    parser.add_argument("--requires-codex-review", action="store_true",
                        help="classifier flag; makes complex reviews prefer codex")
    args = parser.parse_args(argv)
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(route(args.role, args.task_route, config, load_synced_state(config), args.requires_codex_review), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
