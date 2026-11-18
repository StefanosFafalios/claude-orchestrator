import json

import pytest
from conftest import TEMPLATE

from engine_state import ConfigError, load_config
from model_map import UNUSED, canonical, engine_name, row, validate
from test_route_engine import write_models


@pytest.fixture
def config(project):
    return load_config()


def test_supercritical_implementation_uses_critical_row(config):
    assert row(config, "implementation", "supercritical") == row(config, "implementation", "critical")


def test_canonical_reverse_lookup(config):
    assert canonical(config, "claude", "sonnet") == "sonnet"
    assert canonical(config, "codex", "gpt-6-luna") == "gpt-6-luna"
    assert canonical(config, "claude", "claude-fable-5-2") == "fable"
    assert canonical(config, "codex", "nope") is None


def test_codex_name_respects_models_cache(config, usage_files):
    write_models(usage_files, "gpt-6-luna")
    assert engine_name(config, "gpt-6-luna", "codex") == "gpt-6-luna"
    assert engine_name(config, "gpt-6.1-sol", "codex") == ""
    assert engine_name(config, "sonnet", "codex") == ""


def test_unknown_model_in_routing_is_config_error(write_config):
    write_config({"routing": {"review": {"standard": {"models": ["ghost"], "effort": "low"}}}})
    with pytest.raises(ConfigError, match="ghost"):
        load_config()


def test_shipped_template_validates_without_unused_rows():
    template = json.loads(TEMPLATE.read_text())
    assert template["map_meta"]["log"] == ".claude/skills/review-models/decision-log.md"
    for role, route in UNUSED:
        assert route not in template["routing"][role]
    validate(template)


def test_unused_rows_are_ignored(write_config):
    # Configs copied from older templates still carry these rows; they never route.
    write_config({"routing": {role: {route: {"models": ["ghost"], "effort": "low"}} for role, route in UNUSED}})
    load_config()


def test_ask_effort_only_for_supercritical_review_or_architecture(write_config):
    write_config({"routing": {"implementation": {"trivial": {"models": ["haiku"], "effort": "ask"}}}})
    with pytest.raises(ConfigError, match="ask"):
        load_config()


def test_implementation_supercritical_row_is_rejected(write_config):
    write_config({"routing": {"implementation": {"supercritical": {"models": ["opus"], "effort": "high"}}}})
    with pytest.raises(ConfigError, match="supercritical"):
        load_config()


def test_legacy_project_keys_are_reported_not_fatal(write_config):
    write_config({"defaults": {"scout_model": "haiku"}, "codex": {"tiers": {}, "max_files_per_fallback": 3}})
    config = load_config()
    assert config["legacy_keys"] == ["defaults", "codex.tiers"]
    assert config["codex"]["max_files_per_fallback"] == 3
    assert "defaults" not in config and "tiers" not in config["codex"]
