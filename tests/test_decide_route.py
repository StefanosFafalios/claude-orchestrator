"""Cheap tier decider: one OpenRouter call, clamped by the keyword floor, exit 3 on any failure."""

import json
import os
import subprocess
import sys

import decide_route
import openrouter_budget as ob
import pytest
from conftest import ROOT

SCRIPT = ROOT / "scripts" / "decide_route.py"
KEY = "sk-or-v1-0123456789abcdefsecret"


def enable_decider(write_config, monkeypatch, models=None, **extra):
    """OpenRouter on, with a decider row the frozen test map does not carry."""
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    write_config({"engines": {"openrouter": True},
                  "models": {"or-cheap": {"score": 56, "engines": {"openrouter": "vendor/cheap"}}, **(models or {})},
                  "routing": {"decider": {"standard": {"models": ["or-cheap", "haiku"], "effort": "low"}}},
                  **extra})


def decide(project, prompt):
    out = subprocess.run([sys.executable, str(SCRIPT)], input=prompt, capture_output=True, text=True,
                         timeout=20, env=dict(os.environ, CLAUDE_PROJECT_DIR=str(project)))
    return out.returncode, out.stdout, out.stderr


def test_decided_route_is_returned(project, write_config, monkeypatch, fake_openrouter):
    enable_decider(write_config, monkeypatch)
    fake_openrouter.chat_body = fake_openrouter.answer('{"route": "complex", "reason": "two services"}')
    code, out, err = decide(project, "add unit tests for parser")
    assert code == 0, err
    result = json.loads(out)
    assert (result["route"], result["model"]) == ("complex", "vendor/cheap")
    assert "two services" in result["reason"]
    request = json.loads(fake_openrouter.chat_requests[0]["body"])
    assert request["model"] == "vendor/cheap" and request["temperature"] == 0 and request["max_tokens"] <= 300
    text = json.dumps(request["messages"])
    assert "supercritical = needs frontier reasoning" in text and "add unit tests for parser" in text


def test_keyword_floor_clamps_the_decided_route_up(project, write_config, monkeypatch, fake_openrouter):
    enable_decider(write_config, monkeypatch)
    fake_openrouter.chat_body = fake_openrouter.answer('{"route": "trivial", "reason": "small"}')
    code, out, _ = decide(project, "drop the users table")
    assert code == 0 and json.loads(out)["route"] == "critical"


def test_decider_floor_stops_at_critical(project, write_config, monkeypatch, fake_openrouter):
    """Supercritical has no floor: only the session, with the whole conversation, may pick it."""
    enable_decider(write_config, monkeypatch)
    fake_openrouter.chat_body = fake_openrouter.answer('{"route": "supercritical", "reason": "novel proof"}')
    code, out, _ = decide(project, "prove the scheduler never deadlocks")
    result = json.loads(out)
    assert code == 0 and result["route"] == "critical"
    assert "suggests supercritical" in result["reason"] and "novel proof" in result["reason"]


def test_haiku_on_openrouter_when_the_cheapest_model_is_missing(project, write_config, monkeypatch,
                                                                fake_openrouter, usage_files):
    enable_decider(write_config, monkeypatch, models={"haiku": {"score": 40, "engines": {
        "claude": "haiku", "openrouter": "anthropic/haiku"}}})
    (usage_files / "openrouter-models.json").write_text(json.dumps({"models": ["anthropic/haiku"]}))
    code, out, _ = decide(project, "add a button")
    assert code == 0 and json.loads(out)["model"] == "anthropic/haiku"


@pytest.mark.parametrize("case", ["disabled", "no_key", "limited", "near_limit", "spent", "no_row"])
def test_not_usable_falls_back_without_a_call(project, write_config, monkeypatch, fake_openrouter, case):
    enable_decider(write_config, monkeypatch)
    if case == "disabled":
        write_config({"engines": {"openrouter": False}})
    elif case == "no_key":
        monkeypatch.delenv("OPENROUTER_API_KEY")
    elif case == "no_row":
        write_config({"engines": {"openrouter": True}})
    elif case == "spent":
        ob.write_snapshot({"limit_remaining": 0}, __import__("time").time())
    else:
        status = {"limited": ("limited", 0), "near_limit": ("near_limit", 10)}[case]
        (project / ".claude" / "engine-state.json").write_text(
            json.dumps({"openrouter": {"status": status[0], "remaining_pct": status[1]}}))
    code, out, err = decide(project, "add a button")
    assert (code, out) == (3, "") and err.startswith("fallback: session model classifies")
    assert fake_openrouter.chat_requests == []


@pytest.mark.parametrize("status, body", [
    (500, {"error": "boom"}),
    (402, {"error": {"code": 402, "message": "Payment Required"}}),
    (200, "not json"),
    (200, {"choices": [{"message": {"content": "I think it is standard"}}]}),
])
def test_bad_answers_fall_back(project, write_config, monkeypatch, fake_openrouter, status, body):
    enable_decider(write_config, monkeypatch)
    fake_openrouter.chat_status = status
    fake_openrouter.chat_body = body
    code, out, err = decide(project, "add a button")
    assert (code, out) == (3, "") and "fallback" in err
    assert KEY not in err
    assert not (project / ".claude" / "engine-state.json").exists(), "a decider failure marks nothing"


def test_unknown_route_falls_back(project, write_config, monkeypatch, fake_openrouter):
    enable_decider(write_config, monkeypatch)
    fake_openrouter.chat_body = fake_openrouter.answer('{"route": "huge", "reason": "x"}')
    code, _, err = decide(project, "add a button")
    assert code == 3 and "huge" in err


def test_timeout_falls_back(project, write_config, monkeypatch, fake_openrouter, tmp_path, capsys):
    enable_decider(write_config, monkeypatch)
    monkeypatch.setattr(decide_route, "TIMEOUT", 0.2)
    fake_openrouter.chat_delay = 1
    task = tmp_path / "task.txt"
    task.write_text("add a button")
    assert decide_route.main(["--prompt-file", str(task)]) == 3
    assert "fallback" in capsys.readouterr().err


def test_bad_flag_falls_back(capsys):
    assert decide_route.main(["--nope"]) == 3
    assert "fallback" in capsys.readouterr().err


def test_prompt_is_redacted_and_key_only_in_header(project, write_config, monkeypatch, fake_openrouter):
    enable_decider(write_config, monkeypatch)
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz"
    code, out, err = decide(project, f"add a button; my token is {secret} " + "x" * 20000)
    assert code == 0
    request = fake_openrouter.chat_requests[0]
    assert request["headers"]["Authorization"] == f"Bearer {KEY}"
    assert secret not in request["body"] and KEY not in request["body"]
    assert len(request["body"]) < 12000, "the prompt is truncated"
    assert KEY not in out + err


def test_usable_follows_config_and_state(project, write_config, monkeypatch):
    from engine_state import load_config, load_state
    enable_decider(write_config, monkeypatch)
    config = load_config()
    assert decide_route.usable(config, load_state())
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert not decide_route.usable(config, load_state())
