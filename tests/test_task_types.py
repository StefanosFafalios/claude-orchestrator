"""Per-task-type sub-rows (``types``) in routing rows."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import model_map
from engine_state import ConfigError, load_config
from route_engine import route
from test_route_engine import config, state  # noqa: F401  (config is a fixture)

ROOT = Path(__file__).resolve().parent.parent


def with_types(config, role, tier, **types):  # noqa: F811
    config["routing"][role][tier]["types"] = types
    return config


def test_valid_sub_rows_load(write_config):
    write_config({"routing": {"implementation": {"standard": {"models": ["sonnet"], "effort": "low", "types": {
        "frontend": {"models": ["sonnet", "haiku"], "effort": "high", "model_effort": {"haiku": "low"}}}}}}})
    load_config()


@pytest.mark.parametrize("types, message", [
    ({"weird": {"models": ["sonnet"], "effort": "low"}}, "unknown task type"),
    (["frontend"], "'types' must be an object"),
    ({"frontend": {"models": ["nope"], "effort": "low"}}, "unknown model"),
    ({"frontend": {"models": ["sonnet"], "effort": "turbo", "model_effort": {"sonnet": "turbo"}}}, "model_effort"),
    ({"frontend": {"models": ["sonnet"], "effort": "low", "model_effort": {"haiku": "low"}}}, "model_effort"),
    ({"frontend": {"models": ["sonnet"], "effort": "ask"}}, "cannot use effort 'ask'"),
])
def test_invalid_sub_rows_are_config_errors(write_config, types, message):
    write_config({"routing": {"implementation": {"standard": {
        "models": ["sonnet"], "effort": "low", "types": types}}}})
    with pytest.raises(ConfigError, match=message):
        load_config()


def test_type_picks_sub_row_and_unknown_uses_tier_row(config):  # noqa: F811
    tier = route("implementation", "complex", config, state(), "t1")["canonical_model"]
    other = next(m for m in config["models"] if m != tier)
    with_types(config, "implementation", "complex", frontend={"models": [other], "effort": "high"})
    for value in ("frontend", "Frontend", " frontend "):
        assert route("implementation", "complex", config, state(), "t1",
                     task_type=model_map.task_type(value))["canonical_model"] == other
    for value in (None, "general", "weird"):
        assert route("implementation", "complex", config, state(), "t1",
                     task_type=model_map.task_type(value))["canonical_model"] == tier


def test_supercritical_implementation_uses_critical_sub_row(config):  # noqa: F811
    tier = route("implementation", "supercritical", config, state(), "t1")["canonical_model"]
    other = next(m for m in config["models"] if m != tier)
    with_types(config, "implementation", "critical", debug={"models": [other], "effort": "low"})
    assert route("implementation", "supercritical", config, state(), "t1",
                 task_type="debug")["canonical_model"] == other


def test_docs_without_docs_rows_uses_style_review_sub_row(config):  # noqa: F811
    config["routing"].pop("docs", None)
    tier = route("docs", "standard", config, state(), "t1")["canonical_model"]
    other = next(m for m in config["models"] if m != tier)
    with_types(config, "style_review", "standard", docs={"models": [other], "effort": "low"})
    assert route("docs", "standard", config, state(), "t1", task_type="docs")["canonical_model"] == other


def test_weak_review_type_row_still_needs_strong_reviewer(config):  # noqa: F811
    weakest = min(config["models"], key=lambda m: model_map.score(config, m))
    strongest = max(config["models"], key=lambda m: model_map.score(config, m))
    with_types(config, "review", "standard", tests={"models": [weakest], "effort": "low"})
    result = route("review", "standard", config, state(), "t1", strongest, task_type="tests")
    assert result["engine"] == "none" or result["requires_human_review"] or (
        model_map.score(config, result["canonical_model"]) >= model_map.score(config, strongest))


def test_review_auto_writer_uses_the_same_type(config):  # noqa: F811
    tier = route("implementation", "standard", config, state(), "t1")["canonical_model"]
    other = next(m for m in config["models"] if m != tier)
    with_types(config, "implementation", "standard", frontend={"models": [other], "effort": "low"})
    config["routing"]["review"]["standard"]["models"] = [tier, other]
    typed = route("review", "standard", config, state(), "t1", "auto", task_type="frontend")
    untyped = route("review", "standard", config, state(), "t1", "auto")
    assert typed["canonical_model"] == tier  # the writer was `other`
    assert untyped["canonical_model"] == other  # the writer was `tier`


def test_cli_task_type(project, write_config):
    write_config({"routing": {"implementation": {"standard": {"models": ["sonnet"], "effort": "low", "types": {
        "frontend": {"models": ["haiku"], "effort": "low"}}}}}})
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "route_engine.py"), "--role", "implementation",
                          "--task-type", "Frontend"], capture_output=True, text=True, cwd=project)
    assert json.loads(out.stdout)["canonical_model"] == "haiku", out.stderr
