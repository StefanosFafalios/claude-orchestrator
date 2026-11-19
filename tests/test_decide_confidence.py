"""Decider confidence: ask the user when it is unsure; the workflow takes the higher tier."""

import json

import pytest
from test_decide_route import decide, enable_decider

from engine_state import ConfigError, load_config


def answer_with(fake_openrouter, **fields):
    fake_openrouter.chat_body = fake_openrouter.answer(json.dumps({"route": "standard", "reason": "r", **fields}))


@pytest.mark.parametrize("fields, route, alternative, ask", [
    ({"confidence": 0.95, "alternative": "complex"}, "standard", "complex", False),
    ({"confidence": 0.6, "alternative": "trivial"}, "standard", "trivial", True),
    ({"alternative": "complex"}, "standard", "complex", True),  # missing confidence = 0.0
    ({"confidence": "high", "alternative": "complex"}, "standard", "complex", True),
    ({"confidence": 0.6, "alternative": "huge"}, "standard", "complex", True),
    ({"confidence": 0.6, "alternative": "standard"}, "standard", "complex", True),
    ({"confidence": 0.6, "route": "critical", "alternative": "critical"}, "critical", None, False),
    ({"confidence": 0.6, "route": "supercritical"}, "critical", None, False),
])
def test_confidence_and_alternative(project, write_config, monkeypatch, fake_openrouter, fields, route, alternative, ask):
    enable_decider(write_config, monkeypatch)
    answer_with(fake_openrouter, **fields)
    code, out, err = decide(project, "add a button")
    assert code == 0, err
    result = json.loads(out)
    assert (result["route"], result["alternative"], result["ask"]) == (route, alternative, ask)


def test_supercritical_alternative_is_clamped(project, write_config, monkeypatch, fake_openrouter):
    enable_decider(write_config, monkeypatch)
    answer_with(fake_openrouter, route="complex", confidence=0.6, alternative="supercritical")
    result = json.loads(decide(project, "add a button")[1])
    assert (result["alternative"], result["ask"]) == ("critical", True)
    assert "suggests supercritical" in result["reason"]


def test_synthesized_alternative_does_not_suggest_supercritical(project, write_config, monkeypatch, fake_openrouter):
    enable_decider(write_config, monkeypatch)
    answer_with(fake_openrouter, route="critical", confidence=0.95)
    result = json.loads(decide(project, "add a button")[1])
    assert "suggests supercritical" not in result["reason"]


def test_raw_supercritical_route_has_no_alternative(project, write_config, monkeypatch, fake_openrouter):
    enable_decider(write_config, monkeypatch)
    answer_with(fake_openrouter, route="supercritical", confidence=0.5, alternative="complex")
    result = json.loads(decide(project, "add a button")[1])
    assert (result["route"], result["alternative"], result["ask"]) == ("critical", None, False)
    assert "suggests supercritical" in result["reason"]


def test_floor_at_or_above_the_alternative_drops_it(project, write_config, monkeypatch, fake_openrouter):
    enable_decider(write_config, monkeypatch)
    answer_with(fake_openrouter, route="trivial", confidence=0.5, alternative="complex")
    result = json.loads(decide(project, "drop the users table")[1])
    assert (result["route"], result["alternative"], result["ask"]) == ("critical", None, False)


def test_min_confidence_comes_from_config(project, write_config, monkeypatch, fake_openrouter):
    enable_decider(write_config, monkeypatch, decider={"min_confidence": 0.5})
    answer_with(fake_openrouter, confidence=0.6, alternative="complex")
    assert json.loads(decide(project, "add a button")[1])["ask"] is False


@pytest.mark.parametrize("reported, expected", [
    ("Frontend", "frontend"),
    (" tests ", "tests"),
    ("cooking", None),
    (None, None),
    (5, None),  # non-string model output is ignored, not a decider failure
    (["frontend"], None),
])
def test_decider_returns_only_valid_normalized_task_types(project, write_config, monkeypatch, fake_openrouter,
                                                          reported, expected):
    enable_decider(write_config, monkeypatch)
    answer_with(fake_openrouter, type=reported)
    result = json.loads(decide(project, "add a button")[1])
    if expected is None:
        assert "type" not in result
    else:
        assert result["type"] == expected


@pytest.mark.parametrize("value", [0, 1.5, "x", True, -1])
def test_invalid_min_confidence_is_an_invalid_config(write_config, monkeypatch, value):
    enable_decider(write_config, monkeypatch, decider={"min_confidence": value})
    with pytest.raises(ConfigError, match="min_confidence"):
        load_config()
