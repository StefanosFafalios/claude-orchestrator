#!/usr/bin/env python3
"""Choose engine, model and effort for one orchestration role.

Reads the merged configuration and ``.claude/engine-state.json`` and prints JSON:
``{"engine", "model", "effort", "canonical_model", "reason", "requires_human_review",
"scope"}``. ``engine`` is ``claude``, ``codex``, ``openrouter`` or ``none``.

Each role and task route has a routing row of canonical model ids in preference order
(``model_map``); an engine serves a model when the model's ``engines`` map names it.
Reviews take a model other than the implementer's that scores at least as high. With
``balance.mode`` ``adaptive`` and a ``--task-id``, healthy engines share balanced routes
in proportion to their distance from the near-limit line.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

import model_map
from engine_state import ENGINES, ConfigError, effective_status, engine_enabled, load_config, load_synced_state

DEFAULT_BALANCED = ("standard", "complex")


class Engines:
    """Per-engine availability derived from config and state."""

    def __init__(self, config: dict, state: dict):
        self.config = config
        self.state = state
        self.enabled = {e: engine_enabled(config, e) for e in ENGINES}
        self.status = {e: effective_status(state[e], config) for e in ENGINES if self.enabled[e]}

    def remaining(self, engine: str) -> float:
        return float(self.state[engine].get("remaining_pct", 0))

    def headroom(self, engine: str) -> float:
        """Percentage points above the near-limit line."""
        return max(0.0, self.remaining(engine) - self.config["thresholds"]["near_limit_pct"])

    def fallback_allowed(self, src: str, dst: str) -> bool:
        return bool(self.config.get("fallback", {}).get(f"{src}_to_{dst}", True))


def _none(reason: str, human: bool = True) -> dict:
    return {"engine": "none", "model": "", "effort": "", "canonical_model": "", "reason": reason,
            "requires_human_review": human, "scope": "none"}


def _bucket(task_key: str) -> float:
    """Deterministic position in [0, 1) for a task, so the same task always routes the same way."""
    return int(hashlib.sha256(task_key.encode("utf-8")).hexdigest()[:8], 16) / 2**32


def _pick_weighted(engines: Engines, healthy: list[str], task_key: str) -> tuple[str, str]:
    """Deterministic headroom-weighted choice among healthy engines."""
    rooms = [engines.headroom(e) for e in healthy]
    weights = rooms if sum(rooms) else [1.0] * len(healthy)
    point = _bucket(task_key) * sum(weights)
    for engine, weight in zip(healthy, weights):
        point -= weight
        if point < 0:
            break
    detail = ", ".join(f"{e} {r:g} pts" for e, r in zip(healthy, rooms))
    return engine, f"adaptive split (headroom {detail})"


def _select(engines: Engines, model_ids: list[str], task_key: str | None = None, split: bool = False,
            exclude: tuple = (), route_name: str = "") -> dict | None:
    """Pick the engine and model for a preference-ordered model list, or None."""
    config = engines.config
    serving = {}
    for engine in ENGINES:
        model = next((m for m in model_ids if engines.enabled[engine] and model_map.engine_name(config, m, engine)), None)
        if model:
            serving[engine] = model
    if not serving:
        return None
    # The engine the work belongs to when everything is healthy.
    home = min(serving, key=lambda e: (model_ids.index(serving[e]), ENGINES.index(e)))
    away = home in exclude or engines.status[home] != "healthy"
    candidates = {e: m for e, m in serving.items()
                  if engines.status[e] != "limited" and e not in exclude
                  and not (away and e != home and not engines.fallback_allowed(home, e))}
    if not candidates:
        return None
    healthy = [e for e in candidates if engines.status[e] == "healthy"]
    balance = config.get("balance", {})
    if healthy:
        if (split and task_key and balance.get("mode", "adaptive") == "adaptive" and len(healthy) >= 2
                and route_name in balance.get("routes", DEFAULT_BALANCED)):
            if "openrouter" in healthy:
                served_elsewhere = {m for m in model_ids for e in healthy if e != "openrouter"
                                    and model_map.engine_name(config, m, e)}
                candidate = next((m for m in model_ids if m not in served_elsewhere
                                  and model_map.engine_name(config, m, "openrouter")), None)
                if candidate:
                    candidates["openrouter"] = candidate
                else:
                    healthy.remove("openrouter")
            engine, reason = _pick_weighted(engines, healthy, task_key)
        else:
            engine = min(healthy, key=lambda e: model_ids.index(candidates[e]))
            reason = f"{engine} serves {candidates[engine]}" if engine == home else (
                f"{home} is {engines.status[home] if home not in exclude else 'excluded'}; using {engine}")
        return {"engine": engine, "model_id": candidates[engine], "reason": reason}
    engine = max(candidates, key=engines.remaining)
    return {"engine": engine, "model_id": candidates[engine], "scope": "single_atomic_task",
            "reason": f"no healthy engine; {engine} has the most remaining quota ({engines.status[engine]})",
            "requires_human_review": bool(
                config.get("fallback", {}).get("require_human_review_when_both_limited", True))}


def _result(config: dict, picked: dict | None, effort: str, per_model: dict | None = None) -> dict:
    if not picked:
        return _none("no engine available (limited, disabled, excluded or fallback not allowed)")
    model_id = picked["model_id"]
    effort = (per_model or {}).get(model_id, effort)
    return {"engine": picked["engine"], "model": model_map.engine_name(config, model_id, picked["engine"]),
            "effort": effort, "canonical_model": model_id, "reason": picked["reason"],
            "requires_human_review": picked.get("requires_human_review", False),
            "scope": picked.get("scope", "full_task")}


def _implementer(config: dict, name: str) -> str | None:
    """Canonical id for an engine-specific or canonical model name."""
    return next((m for m in (model_map.canonical(config, e, name) for e in ENGINES) if m), None)


def supercritical_effort(state: dict, task_key: str | None, parent_task_id: str | None,
                         permission_mode: str | None, headless: bool) -> tuple[str, bool]:
    """Effort for an ``ask`` row as ``(effort, requires_human_review)``."""
    efforts = state.get("supercritical_efforts", {})
    if task_key and task_key in efforts:
        return efforts[task_key], False
    auto = permission_mode == "auto"
    if auto and parent_task_id and parent_task_id in efforts:
        return efforts[parent_task_id], False
    if headless:
        return ("medium", False) if auto else ("", True)
    return "ask", False


def _need_human(result: dict, human: bool) -> dict:
    if human and result["engine"] != "none":
        result["requires_human_review"] = True
        result["reason"] += "; a human must choose the supercritical effort"
    return result


def route(role: str, task_route: str, config: dict, state: dict, task_key: str | None = None,
          implementation_model: str | list | None = None, exclude: tuple = (), parent_task_id: str | None = None,
          permission_mode: str | None = None, headless: bool = False, task_type: str | None = None) -> dict:
    """``implementation_model`` is one model or every model that wrote code (a fix escalation adds
    one); ``"auto"`` derives the implementer from ``task_key``."""
    if (role, task_route) in model_map.UNUSED:
        return _none(f"trivial tasks get no {role.replace('_', ' ')}", human=False)
    engines = Engines(config, state)
    entry = dict(model_map.row(config, role, task_route, task_type))
    human = False
    per_model = entry.get("model_effort", {})
    if entry["effort"] == "ask":
        entry["effort"], human = supercritical_effort(state, task_key, parent_task_id, permission_mode, headless)
        per_model = {}
    elif (role, task_route) in model_map.ASK_ALLOWED:  # user-named effort: top-model rows only
        efforts = state.get("supercritical_efforts", {})
        if task_key in efforts or parent_task_id in efforts:
            per_model = {}
        entry["effort"] = efforts.get(task_key, efforts.get(parent_task_id, entry["effort"]))
    models = list(entry["models"])
    if role == "implementation":
        return _result(config, _select(engines, models, task_key, True, tuple(exclude), task_route), entry["effort"], per_model)
    if role == "review":
        writers = [implementation_model] if isinstance(implementation_model, str) else list(implementation_model or [])
        writers = [route("implementation", task_route, config, state, task_key, task_type=task_type)["canonical_model"] if w == "auto" else w
                   for w in writers]
        impl_ids = {m for m in (_implementer(config, w) for w in writers if w) if m}
        others = [m for m in models if m not in impl_ids]
        bar = max((model_map.score(config, m) for m in impl_ids), default=None)
        stronger = [m for m in others if model_map.score(config, m) >= bar] if bar is not None else others
        result = _result(config, _select(engines, stronger, exclude=tuple(exclude)), entry["effort"], per_model)
        if result["engine"] == "none" and stronger != others:
            # Nothing at least as capable is available: a weaker, different model beats no review.
            result = _result(config, _select(engines, others, exclude=tuple(exclude)), entry["effort"], per_model)
            if result["engine"] != "none":
                result["requires_human_review"] = True
                result["reason"] += "; no reviewer at least as capable as the implementer"
        return _need_human(result, human)
    result = _result(config, _select(engines, models, exclude=tuple(exclude)), entry["effort"], per_model)
    if role == "style_review":
        # Skipping style review never needs a human: the main review covers it.
        if not config.get("review", {}).get("split", True):
            return _none("covered by the main review", human=False)
        if result["engine"] == "none" or engines.status[result["engine"]] != "healthy":
            return _none("both engines constrained; main review covers style", human=False)
    return _need_human(result, human)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--role", choices=model_map.ROLES, required=True)
    parser.add_argument("--task-route", choices=model_map.ROUTES, default="standard")
    parser.add_argument("--task-id", help="stable task key; enables the adaptive split")
    parser.add_argument("--implementation-model", action="append",
                        help="review only: a model that wrote code (engine or canonical name, or 'auto' to derive it "
                             "from --task-id); repeat for each, e.g. after a fix escalation")
    parser.add_argument("--exclude-engine", action="append", default=[], choices=ENGINES,
                        help="never pick this engine (repeatable)")
    parser.add_argument("--parent-task-id", help="parent task id (supercritical effort)")
    parser.add_argument("--permission-mode", help="permission mode of the session (supercritical effort)")
    parser.add_argument("--headless", action="store_true", help="no human available to answer questions")
    parser.add_argument("--task-type", help="optional type (frontend, debug, refactor, tests, docs); others are ignored")
    args = parser.parse_args(argv)
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result = route(args.role, args.task_route, config, load_synced_state(config), args.task_id,
                   args.implementation_model, tuple(args.exclude_engine), args.parent_task_id,
                   args.permission_mode, args.headless, model_map.task_type(args.task_type))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
