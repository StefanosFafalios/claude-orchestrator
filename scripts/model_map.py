"""Model-centric capability map: models with scores, and routing rows that name models.

Engines are just places a model can run; ``models.<id>.engines`` maps each engine to
its own name for that model, so routing lists canonical ids and stays engine-neutral.
"""

from __future__ import annotations

from engine_state import EFFORTS, ConfigError

ROLES = ("implementation", "review", "scout", "style_review", "architecture", "docs", "decider")
ROUTES = ("trivial", "standard", "complex", "critical", "supercritical")
LEGACY_KEYS = ("defaults", "codex.tiers", "codex.default_model")
ASK_ALLOWED = {("review", "supercritical"), ("architecture", "supercritical")}
TASK_TYPES = ("frontend", "debug", "refactor", "tests", "docs")
# Trivial work is handled directly: nothing routes a scout, a model review or a docs pass for it.
# Copies of older templates still carry these rows, so they are ignored, not rejected.
UNUSED = {("review", "trivial"), ("style_review", "trivial"), ("scout", "trivial"), ("docs", "trivial")}


def legacy_keys(project_config: dict) -> list[str]:
    """Legacy keys present in the project's raw config (dotted for nested ones)."""
    found = []
    for key in LEGACY_KEYS:
        node = project_config
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node is not None:
            found.append(key)
    return found


def _check_row(entry: dict, where: str, models: dict, ask_ok: bool) -> None:
    names = entry.get("models")
    if not isinstance(names, list) or not names or not isinstance(entry.get("effort"), str):
        raise ConfigError(f"{where} needs a non-empty 'models' list and an 'effort'")
    for name in names:
        if name not in models:
            raise ConfigError(f"{where} names unknown model '{name}'")
    if entry["effort"] != "ask" and entry["effort"] not in EFFORTS:
        raise ConfigError(f"{where} 'effort' must be 'ask' or one of {', '.join(EFFORTS)}")
    if entry["effort"] == "ask" and not ask_ok:
        raise ConfigError(f"{where} cannot use effort 'ask'; only review/architecture supercritical can")
    per_model = entry.get("model_effort", {})
    if not isinstance(per_model, dict):
        raise ConfigError(f"{where} 'model_effort' must be an object")
    for name, effort in per_model.items():
        if name not in names:
            raise ConfigError(f"{where} 'model_effort' names '{name}', which is not in its models")
        if effort not in EFFORTS:
            raise ConfigError(f"{where} 'model_effort' for '{name}' must be one of {', '.join(EFFORTS)}")


def validate(config: dict) -> None:
    models = config.get("models")
    routing = config.get("routing")
    if not isinstance(models, dict) or not isinstance(routing, dict):
        raise ConfigError("config needs 'models' and 'routing' objects")
    if "decider" in config:
        minimum = config["decider"].get("min_confidence", 0.9) if isinstance(config["decider"], dict) else None
        if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not 0 < minimum <= 1:
            raise ConfigError("decider.min_confidence must be a number with 0 < x <= 1")
    for role in ROLES:
        if role in ("docs", "decider") and role not in routing:
            continue  # configs from before these roles: docs uses style_review, the decider is off
        rows = routing.get(role)
        if not isinstance(rows, dict):
            raise ConfigError(f"routing.{role} is missing")
        # The decider classifies before any route is known: it has one row, ``standard``.
        for route_name in ("standard",) if role == "decider" else ROUTES:
            where = f"routing.{role}.{route_name}"
            if (role, route_name) == ("implementation", "supercritical"):
                if route_name in rows:
                    raise ConfigError(f"{where} must be absent; supercritical implementation uses the critical row")
                continue
            if (role, route_name) in UNUSED:
                continue
            entry = rows.get(route_name)
            if not isinstance(entry, dict):
                raise ConfigError(f"{where} is missing")
            _check_row(entry, where, models, (role, route_name) in ASK_ALLOWED)
            types = entry.get("types", {})
            if not isinstance(types, dict):
                raise ConfigError(f"{where} 'types' must be an object")
            for type_name, sub in types.items():
                if type_name not in TASK_TYPES:
                    raise ConfigError(f"{where} 'types' names unknown task type '{type_name}'")
                sub_where = f"{where}.types.{type_name}"
                if not isinstance(sub, dict):
                    raise ConfigError(f"{sub_where} is missing")
                _check_row(sub, sub_where, models, (role, route_name) in ASK_ALLOWED)


def task_type(value: str | None) -> str | None:
    """Normalized task type, or None when absent, ``general`` or unknown (the tier row applies)."""
    value = value.strip().lower() if isinstance(value, str) else ""  # model output may be any JSON
    return value if value in TASK_TYPES else None


def has_type_rows(config: dict) -> bool:
    return any(isinstance(entry, dict) and "types" in entry
               for role in ROLES for rows in (config["routing"].get(role),) if isinstance(rows, dict)
               for route, entry in rows.items()
               if (role, route) not in UNUSED)


_normalize = task_type  # ``row`` has a parameter that shadows the function


def row(config: dict, role: str, route: str, task_type: str | None = None) -> dict:
    if (role, route) == ("implementation", "supercritical"):
        route = "critical"
    if role == "decider":
        route = "standard"
    if role == "docs" and role not in config["routing"]:
        role = "style_review"
    entry = config["routing"][role][route]
    return entry.get("types", {}).get(_normalize(task_type), entry)


def score(config: dict, model_id: str) -> float:
    return float(config["models"][model_id]["score"])


def canonical(config: dict, engine: str, name: str) -> str | None:
    """Canonical id for an engine-specific name (a canonical id is accepted too)."""
    for model_id, model in config["models"].items():
        if name == model_id or model.get("engines", {}).get(engine) == name:
            return model_id
    return None


def engine_name(config: dict, model_id: str, engine: str) -> str:
    """The engine's name for the model, or ``""`` when not offered or not available."""
    name = config["models"].get(model_id, {}).get("engines", {}).get(engine, "")
    if name and engine == "codex":
        import usage_sources  # lazy: keeps this module importable without the usage readers
        available = usage_sources.codex_models()
        if available is not None and name not in available:
            return ""
    if name and engine == "openrouter":
        import usage_sources
        available = usage_sources.openrouter_models()
        if available is not None and name not in available:
            return ""
    return name
