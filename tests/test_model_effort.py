"""Per-model effort overrides (``model_effort``) in routing rows."""
import pytest

from engine_state import ConfigError, load_config
from route_engine import route
from test_route_engine import config, state  # noqa: F401  (config is a fixture)


def test_valid_map_and_absent_map_load(write_config):
    write_config({"routing": {"implementation": {"standard": {
        "models": ["sonnet", "haiku"], "effort": "medium", "model_effort": {"sonnet": "low", "haiku": "max"}}}}})
    load_config()


@pytest.mark.parametrize("bad", [{"opus": "low"}, {"sonnet": "turbo"}, {"sonnet": "ask"}, ["sonnet"]])
def test_invalid_map_is_config_error(write_config, bad):
    write_config({"routing": {"implementation": {"standard": {
        "models": ["sonnet"], "effort": "medium", "model_effort": bad}}}})
    with pytest.raises(ConfigError, match="model_effort"):
        load_config()


def test_override_follows_the_picked_model(config):  # noqa: F811
    row = config["routing"]["implementation"]["standard"]
    first = route("implementation", "standard", config, state(), "t1")
    assert first["effort"] == row["effort"]
    row["model_effort"] = {first["canonical_model"]: "xhigh"}
    assert route("implementation", "standard", config, state(), "t1")["effort"] == "xhigh"
    # A fallback pick gets its own override; a model without one keeps the row effort.
    limited = state(**{first["engine"]: "limited"})
    second = route("implementation", "standard", config, limited, "t1")
    assert second["canonical_model"] != first["canonical_model"]
    assert second["effort"] == row["effort"]
    row["model_effort"][second["canonical_model"]] = "low"
    assert route("implementation", "standard", config, limited, "t1")["effort"] == "low"


def test_review_honors_override(config):  # noqa: F811
    row = config["routing"]["review"]["standard"]
    picked = route("review", "standard", config, state(), "t1", "sonnet")
    row["model_effort"] = {picked["canonical_model"]: "high"}
    assert route("review", "standard", config, state(), "t1", "sonnet")["effort"] == "high"


def test_user_named_effort_and_ask_beat_override(config):  # noqa: F811
    row = config["routing"]["review"]["supercritical"]
    picked = route("review", "supercritical", config, state(), "t1")["canonical_model"]
    row["model_effort"] = {picked: "low"}
    assert route("review", "supercritical", config, state(), "t1")["effort"] == "low"
    named = {**state(), "supercritical_efforts": {"t1": "high"}}
    assert route("review", "supercritical", config, named, "t1")["effort"] == "high"
    row["effort"] = "ask"
    assert route("review", "supercritical", config, state(), "t2")["effort"] == "ask"
