"""OpenRouter single-call review: one request, redacted, size-capped, research-runner exit codes."""

import json

import pytest
import review_run

KEY = "sk-or-v1-0123456789abcdefsecret"


@pytest.fixture
def enabled(write_config, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-4-carbon")
    write_config({"engines": {"openrouter": True}})


def run(tmp_path, capsys, request="Task: add x\n\ndiff --git a/x.py b/x.py\n+x = 1\n"):
    path = tmp_path / "review.md"
    path.write_text(request)
    code = review_run.main([str(path)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_one_call_returns_findings_and_cost(enabled, fake_openrouter, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OPENROUTER_EFFORT", "medium")
    fake_openrouter.chat_body = {"choices": [{"message": {"content": "- high x.py:1 correctness: ..."}}], "usage": {"cost": 0.031}}
    code, out, err = run(tmp_path, capsys, f"Task\n+token = '{KEY}'\n")
    assert code == 0, err
    assert out.startswith("- high x.py:1") and out.rstrip().endswith("Cost: $0.0310")
    assert len(fake_openrouter.chat_requests) == 1
    sent = fake_openrouter.chat_requests[0]
    body = json.loads(sent["body"])
    assert body["model"] == "google/gemini-4-carbon" and body["reasoning"] == {"effort": "medium"}
    assert body["max_tokens"] == review_run.MAX_TOKENS
    assert "cannot open files" in body["messages"][0]["content"]
    assert KEY not in sent["body"] and sent["headers"]["Authorization"] == f"Bearer {KEY}"


def test_oversized_request_is_refused_without_a_call(enabled, fake_openrouter, tmp_path, capsys):
    code, _, err = run(tmp_path, capsys, "x" * (review_run.MAX_REQUEST_CHARS + 1))
    assert code == 1 and "split the diff" in err and not fake_openrouter.chat_requests


@pytest.mark.parametrize("status, code", [(402, 42), (500, 1)])
def test_http_errors(enabled, fake_openrouter, tmp_path, capsys, status, code):
    fake_openrouter.chat_status, fake_openrouter.chat_body = status, {"error": {"message": "nope"}}
    assert run(tmp_path, capsys)[0] == code


def test_empty_review_exits_1(enabled, fake_openrouter, tmp_path, capsys):
    fake_openrouter.chat_body = {"choices": [{"message": {"content": "  "}}]}
    assert run(tmp_path, capsys)[0] == 1


def test_disabled_openrouter_makes_no_call(fake_openrouter, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-4-carbon")
    assert run(tmp_path, capsys)[0] == 1 and not fake_openrouter.chat_requests


def test_bad_usage_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    assert run(tmp_path, capsys)[0] == 2
    monkeypatch.setenv("OPENROUTER_MODEL", "m")
    monkeypatch.setenv("OPENROUTER_EFFORT", "turbo")
    assert run(tmp_path, capsys)[0] == 2


def test_truncated_review_is_flagged(enabled, fake_openrouter, tmp_path, capsys):
    fake_openrouter.chat_body = {"choices": [{"message": {"content": "- low a.py:1 ..."}, "finish_reason": "length"}]}
    code, out, _ = run(tmp_path, capsys)
    assert code == 0 and "TRUNCATED" in out
