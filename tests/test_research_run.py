"""OpenRouter research runner: report with sources, redaction, exit codes, no call when not usable."""

import json

import openrouter_budget as ob
import pytest
import research_run
from conftest import TEMPLATE

from engine_state import load_config
from route_engine import route
from test_route_engine import state

KEY = "sk-or-v1-0123456789abcdefsecret"


@pytest.fixture
def enabled(write_config, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    write_config({"engines": {"openrouter": True}})


def run(tmp_path, capsys, query="What changed in the latest Python release?", *extra):
    path = tmp_path / "query.txt"
    path.write_text(query)
    code = research_run.main(["--query-file", str(path), "--model", "perplexity/sonar", *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_report_lists_sources_and_cost(enabled, fake_openrouter, tmp_path, capsys):
    fake_openrouter.chat_body = {"choices": [{"message": {"content": "Python 3.14 shipped (2025-10).", "annotations": [
        {"type": "url_citation", "url_citation": {"url": "https://a.example/x", "title": "A"}}]}}],
        "citations": ["https://b.example/y", "https://a.example/x"], "usage": {"cost": 0.0123}}
    code, out, err = run(tmp_path, capsys, "q", "--effort", "low")
    assert code == 0, err
    assert out.startswith("Python 3.14 shipped") and out.rstrip().endswith("Cost: $0.0123")
    assert out.count("https://a.example/x") == 1 and "Sources:\n- https://b.example/y" in out
    request = json.loads(fake_openrouter.chat_requests[0]["body"])
    assert request["model"] == "perplexity/sonar" and request["reasoning"] == {"effort": "low"}
    assert "sources" in request["messages"][0]["content"].lower()


def test_no_citations_means_no_sources_or_cost(enabled, fake_openrouter, tmp_path, capsys):
    fake_openrouter.chat_body = fake_openrouter.answer("plain answer")
    assert run(tmp_path, capsys)[:2] == (0, "plain answer\n")


def test_query_is_redacted_capped_and_key_only_in_header(enabled, fake_openrouter, tmp_path, capsys):
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz"
    code, out, err = run(tmp_path, capsys, f"token {secret} " + "x" * 5000)
    assert code == 0
    request = fake_openrouter.chat_requests[0]
    assert request["headers"]["Authorization"] == f"Bearer {KEY}"
    assert secret not in request["body"] and KEY not in request["body"]
    user = json.loads(request["body"])["messages"][1]["content"]
    assert "[REDACTED]" in user and len(user) == research_run.MAX_QUERY_CHARS
    assert KEY not in out + err


@pytest.mark.parametrize("status, code", [(402, 42), (403, 1), (500, 1)])
def test_http_errors(enabled, fake_openrouter, tmp_path, capsys, status, code):
    fake_openrouter.chat_status = status
    fake_openrouter.chat_body = {"error": {"message": "boom"}}
    got, out, err = run(tmp_path, capsys)
    assert (got, out) == (code, "") and str(status) in err and KEY not in err


def test_key_limit_exits_42(enabled, fake_openrouter, tmp_path, capsys):
    fake_openrouter.chat_status = 403
    fake_openrouter.chat_body = {"error": {"message": "Key limit exceeded"}}
    assert run(tmp_path, capsys)[0] == 42


@pytest.mark.parametrize("body", ["not json", {"choices": []}, {"choices": [{"message": {"content": " "}}]}])
def test_bad_responses_exit_1(enabled, fake_openrouter, tmp_path, capsys, body):
    fake_openrouter.chat_body = body
    assert run(tmp_path, capsys)[:2] == (1, "")


def test_timeout_exits_1(enabled, fake_openrouter, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(research_run, "TIMEOUT", 0.2)
    fake_openrouter.chat_delay = 1
    code, out, err = run(tmp_path, capsys)
    assert (code, out) == (1, "") and "failed" in err


@pytest.mark.parametrize("case", ["disabled", "no_key", "limited", "near_limit", "spent"])
def test_refuses_without_a_call(enabled, fake_openrouter, tmp_path, capsys, project, write_config, monkeypatch, case):
    if case == "disabled":
        write_config({"engines": {"openrouter": False}})
    elif case == "no_key":
        monkeypatch.delenv("OPENROUTER_API_KEY")
    elif case == "spent":
        ob.write_snapshot({"limit_remaining": 0}, __import__("time").time())
    else:
        status = {"limited": ("limited", 0), "near_limit": ("near_limit", 10)}[case]
        (project / ".claude" / "engine-state.json").write_text(
            json.dumps({"openrouter": {"status": status[0], "remaining_pct": status[1]}}))
    code, out, err = run(tmp_path, capsys)
    assert (code, out) == (1, "") and err.startswith("research:")
    assert fake_openrouter.chat_requests == []


def test_bad_usage_exits_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        research_run.main(["--model", "perplexity/sonar"])
    assert exc.value.code == 2
    assert run(tmp_path, capsys, "  ")[0] == 2


def test_research_routes_to_the_cheapest_available_engine(write_config, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)  # OpenRouter counts as enabled only with a key
    write_config({"engines": {"openrouter": True}})
    config = load_config()
    healthy = route("research", "standard", config, state())
    assert (healthy["engine"], healthy["canonical_model"]) == ("claude", "haiku")
    limited = route("research", "standard", config, state(claude="limited", codex="limited"))
    assert (limited["engine"], limited["model"]) == ("openrouter", "perplexity/sonar")
    assert route("research", "trivial", config, state())["engine"] == "none"


def test_missing_research_row_routes_none(write_config):
    config = json.loads(TEMPLATE.read_text())
    del config["routing"]["research"]
    assert route("research", "standard", config, state())["engine"] == "none"
