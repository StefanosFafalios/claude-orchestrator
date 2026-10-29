"""End-to-end workflow scenarios with mocked agents and a fake Codex CLI."""

import json
import shutil
import subprocess

import pytest
from conftest import ROOT

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

DONE = {"status": "done", "summary": "implemented", "files_changed": ["app.py"], "completed_work": "all",
        "remaining_work": "", "usage_limited": False}
NO_FINDINGS = {"findings": []}


@pytest.fixture
def repo(project, monkeypatch, tmp_path_factory):
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    bin_dir = tmp_path_factory.mktemp("bin")
    codex = bin_dir / "codex"
    # Fake Codex: behaviour chosen by FAKE_CODEX=ok|limit.
    codex.write_text('#!/bin/sh\nif [ "$FAKE_CODEX" = limit ]; then echo "You have hit your usage limit" >&2; exit 1; fi\n'
                     'echo "changed app.py" > app.py\necho "codex done"\n')
    codex.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(codex))
    return project


def run(project, scenario):
    out = subprocess.run(["node", str(ROOT / "tests" / "workflow_harness.mjs"), str(project), json.dumps(scenario)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def labels(calls):
    return [c["label"] for c in calls]


def set_state(project, **engines):
    data = {name: {"status": status, "remaining_pct": 0 if status == "limited" else 100} for name, status in engines.items()}
    (project / ".claude" / "engine-state.json").write_text(json.dumps(data))


def state(project):
    return json.loads((project / ".claude" / "engine-state.json").read_text())


def test_standard_task_on_claude(repo):
    out = run(repo, {"task": "add unit tests for parser", "agents": {
        "scout": {"files": ["parser.py"], "impact": "small"}, "implement:claude": DONE, "review:claude": NO_FINDINGS}})
    result, calls = out["result"], out["calls"]
    assert (result["route"], result["implementation_engine"], result["implementation_model"]) == ("standard", "claude", "sonnet")
    assert result["review_engine"] == "claude"
    assert result["fallback_events"] == []
    assert "plan" not in labels(calls)
    impl = next(c for c in calls if c["label"] == "implement:claude")
    assert impl["agentType"] == "cost-aware-orchestrator:implementer" and impl["effort"] == "medium"


def test_trivial_task_skips_scout_and_plan(repo):
    out = run(repo, {"task": "fix typo in README", "agents": {"implement:claude": DONE, "review:claude": NO_FINDINGS}})
    assert out["result"]["implementation_model"] == "haiku"
    assert "scout" not in labels(out["calls"]) and "plan" not in labels(out["calls"])


def test_critical_task_plans_and_reviews_on_codex(repo):
    out = run(repo, {"task": "Refactor the authentication service and add tests", "agents": {
        "scout": {"files": ["auth.py"], "impact": "auth"},
        "plan": {"assessment": "a", "steps": ["s1"], "risks": ["r"], "test_strategy": "t"},
        "implement:claude": DONE,
        "review:codex": {"exit_code": 0, "findings": [{"severity": "medium", "title": "missing test"}]}}})
    result = out["result"]
    assert result["route"] == "critical" and result["implementation_model"] == "opus"
    assert result["review_engine"] == "codex"
    assert result["review_findings"][0]["title"] == "missing test"
    assert "plan" in labels(out["calls"])


def test_claude_limited_routes_implementation_to_codex(repo):
    set_state(repo, claude="limited")
    out = run(repo, {"task": "add unit tests for parser", "agents": {"review:codex": NO_FINDINGS}})
    result = out["result"]
    assert result["implementation_engine"] == "codex"
    assert result["files_changed"] == ["app.py"]
    assert result["review_engine"] == "codex"
    handoff = (repo / result["handoffs"][0]).read_text()
    assert "## Replacement engine\ncodex" in handoff


def test_claude_usage_limit_mid_task_hands_remaining_work_to_codex(repo):
    limited = dict(DONE, status="partial", completed_work="Wrote parser tests for dates",
                   remaining_work="Write parser tests for numbers", usage_limited=True)
    out = run(repo, {"task": "add unit tests for parser", "agents": {
        "scout": {"files": ["parser.py"], "impact": "small"}, "implement:claude": limited, "review:codex": NO_FINDINGS}})
    result = out["result"]
    assert result["implementation_engine"] == "codex"
    assert result["fallback_events"][0]["from"] == "claude" and result["fallback_events"][0]["to"] == "codex"
    assert state(repo)["claude"]["status"] == "limited"
    handoff = (repo / result["fallback_events"][0]["handoff"]).read_text()
    assert "## Remaining work\nWrite parser tests for numbers" in handoff
    assert "## Do not redo\nWrote parser tests for dates" in handoff


def test_codex_usage_limit_hands_off_to_claude(repo, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX", "limit")
    set_state(repo, claude="near_limit")
    out = run(repo, {"task": "add unit tests for parser", "agents": {"implement:claude": DONE, "review:claude": NO_FINDINGS}})
    result, calls = out["result"], out["calls"]
    assert result["fallback_events"][0]["from"] == "codex" and result["fallback_events"][0]["to"] == "claude"
    assert result["implementation_engine"] == "claude"
    assert state(repo)["codex"]["status"] == "limited"
    impl = next(c for c in calls if c["label"] == "implement:claude")
    assert "handoffs/" in impl["prompt"] and "Do not redo" in impl["prompt"]
    assert "add unit tests for parser" not in impl["prompt"], "fallback engine must get the handoff, not the broad prompt"


def test_both_engines_limited_stops(repo):
    set_state(repo, claude="limited", codex="limited")
    out = run(repo, {"task": "add unit tests for parser", "agents": {}})
    assert out["result"]["implementation_engine"] == "none"
    assert out["result"]["requires_human_review"] is True
    assert all(c["kind"] == "bash" for c in out["calls"])


def test_codex_review_limit_falls_back_to_claude_review(repo):
    out = run(repo, {"task": "rotate production credentials", "agents": {
        "scout": {"files": [], "impact": "x"},
        "plan": {"assessment": "a", "steps": ["s"], "risks": [], "test_strategy": "t"},
        "implement:claude": DONE,
        "review:codex": {"exit_code": 42, "findings": []},
        "review:claude": {"findings": [{"severity": "blocker", "title": "no rollback"}]}}})
    result = out["result"]
    assert result["review_engine"] == "claude"
    assert result["fallback_events"][-1]["phase"] == "review"
    assert result["requires_human_review"] is True


def test_configured_tests_run(repo, write_config):
    write_config({"commands": {"test": "echo tests-ran; exit 3"}})
    out = run(repo, {"task": "add unit tests for parser", "agents": {
        "scout": {"files": [], "impact": "x"}, "implement:claude": DONE, "review:claude": NO_FINDINGS}})
    tests = out["result"]["tests"]
    assert tests["exit_code"] == 3 and tests["passed"] is False and "tests-ran" in tests["output_tail"]
