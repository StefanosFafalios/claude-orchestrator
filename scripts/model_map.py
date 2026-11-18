"""Model-centric capability map: models with scores, and routing rows that name models.

Engines are just places a model can run; ``models.<id>.engines`` maps each engine to
its own name for that model, so routing lists canonical ids and stays engine-neutral.
"""

from __future__ import annotations

from engine_state import ConfigError

ROLES = ("implementation", "review", "scout", "style_review", "architecture")
ROUTES = ("trivial", "standard", "complex", "critical", "supercritical")
LEGACY_KEYS = ("defaults", "codex.tiers", "codex.default_model")
ASK_ALLOWED = {("review", "supercritical"), ("architecture", "supercritical")}


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


def validate(config: dict) -> None:
    models = config.get("models")
    routing = config.get("routing")
    if not isinstance(models, dict) or not isinstance(routing, dict):
        raise ConfigError("config needs 'models' and 'routing' objects")
    for role in ROLES:
        rows = routing.get(role)
        if not isinstance(rows, dict):
            raise ConfigError(f"routing.{role} is missing")
        for route_name in ROUTES:
            where = f"routing.{role}.{route_name}"
            if (role, route_name) == ("implementation", "supercritical"):
                if route_name in rows:
                    raise ConfigError(f"{where} must be absent; supercritical implementation uses the critical row")
                continue
            entry = rows.get(route_name)
            if not isinstance(entry, dict):
                raise ConfigError(f"{where} is missing")
            names = entry.get("models")
            if not isinstance(names, list) or not names or not isinstance(entry.get("effort"), str):
                raise ConfigError(f"{where} needs a non-empty 'models' list and an 'effort'")
            for name in names:
                if name not in models:
                    raise ConfigError(f"{where} names unknown model '{name}'")
            if entry["effort"] == "ask" and (role, route_name) not in ASK_ALLOWED:
                raise ConfigError(f"{where} cannot use effort 'ask'; only review/architecture supercritical can")


def row(config: dict, role: str, route: str) -> dict:
    if (role, route) == ("implementation", "supercritical"):
        route = "critical"
    return config["routing"][role][route]


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
    # ponytail: openrouter availability is trusted until usage_sources.openrouter_models() exists (Task 6)
    return name
