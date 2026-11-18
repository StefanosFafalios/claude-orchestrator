"""End-to-end workflow scenarios with mocked agents and a fake Codex CLI."""

import json
import shutil
import subprocess
import sys
import threading
import time

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
    # Fake Codex: behaviour chosen by FAKE_CODEX=ok|limit; FAKE_SLEEP delays it.
    codex.write_text('#!/bin/sh\nif [ "$FAKE_CODEX" = limit ]; then echo "You have hit your usage limit" >&2; exit 1; fi\n'
                     'sleep "${FAKE_SLEEP:-0}"\necho "changed app.py" > app.py\necho "codex done"\n')
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


def test_review_uses_capability_not_engine(repo, write_config):
    write_config({"balance": {"mode": "off"}})
    out = run(repo, {"task": "add unit tests for parser", "agents": {
        "scout": {"files": ["parser.py"], "impact": "small"}, "implement:claude": DONE, "review:codex": NO_FINDINGS}})
    result, calls = out["result"], out["calls"]
    assert (result["route"], result["implementation_engine"], result["implementation_model"]) == ("standard", "claude", "sonnet")
    assert result["review_engine"] == "codex", "gpt-6.1-terra is the first reviewer at least as capable as sonnet"
    review = next(c for c in calls if c["label"] == "review:codex")
    assert "codex_with_fallback.sh" in review["prompt"] and "gpt-6.1-terra" in review["prompt"]
    assert result["fallback_events"] == []
    assert "plan" not in labels(calls)
    impl = next(c for c in calls if c["label"] == "implement:claude")
    assert impl["agentType"] == "cost-aware-orchestrator:implementer" and impl["effort"] == "medium"


STYLE_FINDING = {"findings": [{"severity": "low", "category": "reduction", "title": "dead helper"}]}


def test_style_review_runs_in_parallel_and_merges(repo, write_config):
    write_config({"commands": {"lint": "echo lint-ok"}, "balance": {"mode": "off"}})
    out = run(repo, {"task": "add unit tests for parser", "agents": {
        "scout": {"files": ["parser.py"], "impact": "small"}, "implement:claude": DONE,
        "review:codex": {"exit_code": 0, "findings": [{"severity": "medium", "category": "correctness", "title": "bug"}]},
        "review:style": STYLE_FINDING}})
    style = next(c for c in out["calls"] if c["label"] == "review:style")
    assert (style["agentType"], style["model"], style["effort"]) == ("cost-aware-orchestrator:style-reviewer", "haiku", "low")
    assert "lint-ok" in style["prompt"]
    lint = out["result"]["lint"]
    assert (lint["command"], lint["exit_code"], lint["passed"]) == ("echo lint-ok", 0, True) and "lint-ok" in lint["output_tail"]
    assert [f["severity"] for f in out["result"]["review_findings"]] == ["medium", "low"]
    assert [f["title"] for f in out["result"]["review_findings"]] == ["bug", "dead helper"]
    main = next(c for c in out["calls"] if c["label"] == "review:codex")
    assert "style review is skipped" not in main["prompt"]


SCOUT_AGENTS = {"scout": {"files": ["parser.py"], "impact": "small"}, "implement:claude": DONE,
                "review:codex": NO_FINDINGS, "review:claude": NO_FINDINGS, "review:style": NO_FINDINGS,
                "review:style:openrouter": dict(NO_FINDINGS, exit_code=0)}


def or_first_scout_and_style(write_config, monkeypatch, task_route):
    """OpenRouter on, with the scout and style rows led by an OpenRouter-only model."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_POLL_SECONDS", "0.2")
    cheap = {"models": ["or-cheap", "haiku"], "effort": "low"}
    write_config({"engines": {"openrouter": True}, "balance": {"mode": "off"},
                  "models": {"or-cheap": {"score": 60, "engines": {"openrouter": "vendor/cheap"}}},
                  "routing": {"scout": {task_route: cheap}, "style_review": {task_route: cheap}}})
    return {"route": task_route, "reason": "test"}


@pytest.mark.parametrize("task_route", ["standard", "complex"])
def test_scout_and_style_review_run_on_openrouter(repo, write_config, monkeypatch, fake_openrouter, task_route):
    classification = or_first_scout_and_style(write_config, monkeypatch, task_route)
    out = run(repo, {"task": "add unit tests for parser", "args": {"classification": classification}, "agents": SCOUT_AGENTS})
    calls = {c["label"]: c for c in out["calls"]}
    assert out["result"]["route"] == task_route
    assert "scout" not in calls and calls["scout:openrouter"]["exit_code"] == 0
    assert list(repo.joinpath(*RUNS).iterdir()), "the scout ran through openrouter_run.py"
    assert "Scout findings (vendor/cheap):" in calls["implement:claude"]["prompt"]
    style = calls["review:style:openrouter"]
    assert style["agentType"] == "cost-aware-orchestrator:codex-reviewer" and "review:style" not in calls
    assert "openrouter_run.py" in style["prompt"] and "vendor/cheap" in style["prompt"]
    assert "This is the style review" in style["prompt"]


def test_scout_usage_limit_reroutes_to_claude(repo, write_config, monkeypatch, fake_openrouter):
    fake_openrouter.key_data["limit_remaining"] = 0  # the runner refuses with exit 42
    classification = or_first_scout_and_style(write_config, monkeypatch, "standard")
    out = run(repo, {"task": "add unit tests for parser", "args": {"classification": classification}, "agents": SCOUT_AGENTS})
    calls = {c["label"]: c for c in out["calls"]}
    assert calls["scout:openrouter"]["exit_code"] == 42
    assert (calls["scout"]["agentType"], calls["scout"]["model"]) == ("cost-aware-orchestrator:scout", "haiku")
    assert {"phase": "scout", "from": "openrouter", "to": "claude", "reason": "openrouter usage limit"} in out["result"]["fallback_events"]
    assert state(repo)["openrouter"]["status"] == "limited"
    assert calls["review:style"]["model"] == "haiku", "style review re-routed away from the limited engine"


def test_claude_only_scout_and_style_use_agents(repo, write_config):
    write_config({"engines": {"codex": False}})
    out = run(repo, {"task": "add unit tests for parser", "agents": SCOUT_AGENTS})
    calls = {c["label"]: c for c in out["calls"]}
    assert (calls["scout"]["agentType"], calls["scout"]["model"]) == ("cost-aware-orchestrator:scout", "haiku")
    assert (calls["review:style"]["agentType"], calls["review:style"]["model"]) == ("cost-aware-orchestrator:style-reviewer", "haiku")


DOCS = {"changed": True, "files": ["CLAUDE.md"], "reason": "new command"}
NO_DOCS = {"engine": "none", "changed": False, "files": []}


def test_standard_task_ends_with_doc_updater(repo, write_config):
    write_config({"engines": {"codex": False}})
    out = run(repo, {"task": "add unit tests for parser", "agents": dict(SCOUT_AGENTS, docs=DOCS)})
    docs = out["calls"][-1]
    assert (docs["label"], docs["agentType"], docs["model"], docs["effort"]) == (
        "docs", "cost-aware-orchestrator:doc-updater", "haiku", "low")
    assert "add unit tests for parser" in docs["prompt"] and "app.py" in docs["prompt"]
    assert out["result"]["docs"] == {"engine": "claude", "changed": True, "files": ["CLAUDE.md"]}


def openrouter_docs(write_config, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_POLL_SECONDS", "0.2")
    write_config({"engines": {"openrouter": True}, "balance": {"mode": "off"},
                  "models": {"or-cheap": {"score": 60, "engines": {"openrouter": "vendor/cheap"}}},
                  "routing": {"docs": {"standard": {"models": ["or-cheap", "haiku"], "effort": "low"}}}})


def test_doc_updater_runs_on_openrouter_with_agent_rules(repo, write_config, monkeypatch, fake_openrouter):
    openrouter_docs(write_config, monkeypatch)
    out = run(repo, {"task": "add unit tests for parser", "agents": dict(SCOUT_AGENTS, docs=DOCS)})
    calls = {c["label"]: c for c in out["calls"]}
    assert "docs" not in calls and calls["docs:openrouter"]["exit_code"] == 0
    command = calls["docs:openrouter"]["command"]
    assert "OPENROUTER_SANDBOX=workspace-write" in command and "openrouter_run.py" in command and "vendor/cheap" in command
    rules = (ROOT / "agents" / "doc-updater.md").read_text().split("---\n", 2)[2].strip()
    prompt = (repo / ".claude" / "orchestrator" / "runs" / out["result"]["task_id"] / "docs.md").read_text()
    assert rules in prompt, "external engines get the doc-updater rules verbatim"
    assert "add unit tests for parser" in prompt
    # The fake engine prints no JSON and changes no file beyond the implementation's.
    assert out["result"]["docs"] == {"engine": "openrouter", "changed": False, "files": []}


def test_doc_updater_usage_limit_reroutes_to_claude(repo, write_config, monkeypatch, fake_openrouter):
    fake_openrouter.key_data["limit_remaining"] = 0  # the runner refuses with exit 42
    openrouter_docs(write_config, monkeypatch)
    out = run(repo, {"task": "add unit tests for parser", "agents": dict(SCOUT_AGENTS, docs=DOCS)})
    calls = {c["label"]: c for c in out["calls"]}
    assert calls["docs:openrouter"]["exit_code"] == 42
    assert calls["docs"]["agentType"] == "cost-aware-orchestrator:doc-updater"
    assert {"phase": "docs", "from": "openrouter", "to": "claude", "reason": "openrouter usage limit"} in out["result"]["fallback_events"]
    assert state(repo)["openrouter"]["status"] == "limited"
    assert out["result"]["docs"]["engine"] == "claude"


def test_failed_tests_skip_doc_updater(repo, write_config):
    write_config({"engines": {"codex": False}, "commands": {"test": "echo FAIL; exit 1"}})
    agents = dict(SCOUT_AGENTS, docs=DOCS, **{"implement:claude": [DONE, DONE, DONE]})
    out = run(repo, {"task": "add unit tests for parser", "agents": agents})
    assert out["result"]["tests"]["passed"] is False
    assert "docs" not in labels(out["calls"]) and out["result"]["docs"] == NO_DOCS


def test_constrained_scout_is_not_run(repo, write_config):
    # Near the limit the router asks for one atomic task and a human: not worth a scout.
    write_config({"engines": {"codex": False}})
    (repo / ".claude" / "engine-state.json").write_text(json.dumps({"claude": {"status": "near_limit", "remaining_pct": 20}}))
    out = run(repo, {"task": "add unit tests for parser", "agents": SCOUT_AGENTS})
    assert "scout" not in labels(out["calls"]) and "implement:claude" in labels(out["calls"])


def test_unsplit_review_skips_style_agent(repo, write_config):
    write_config({"review": {"split": False}, "balance": {"mode": "off"}})
    out = run(repo, {"task": "add unit tests for parser", "agents": {
        "scout": {"files": ["parser.py"], "impact": "small"}, "implement:claude": DONE, "review:codex": NO_FINDINGS}})
    assert "review:style" not in labels(out["calls"])
    main = next(c for c in out["calls"] if c["label"] == "review:codex")
    assert "The style review is skipped: also cover standards, best-practice and reduction." in main["prompt"]


def test_failed_style_agent_is_not_a_failure(repo):
    out = run(repo, {"task": "add unit tests for parser", "agents": {
        "scout": {"files": ["parser.py"], "impact": "small"}, "implement:claude": DONE, "review:codex": NO_FINDINGS}})
    assert "review:style" in labels(out["calls"])
    assert out["result"]["review_findings"] == [] and out["result"]["requires_human_review"] is True
    assert out["result"]["lint"] == {"skipped": True}


def test_trivial_task_skips_scout_plan_and_review(repo, write_config):
    write_config({"commands": {"lint": "echo lint-bad; exit 1"}})
    out = run(repo, {"task": "fix typo in README", "agents": {"implement:claude": DONE, "review:claude": NO_FINDINGS}})
    result = out["result"]
    assert result["implementation_model"] == "haiku"
    assert not [c for c in labels(out["calls"]) if c in ("scout", "plan", "docs") or c.startswith(("review:", "route:docs"))]
    assert result["docs"] == NO_DOCS
    assert result["review_engine"] == "none"
    assert result["lint"]["passed"] is False and result["requires_human_review"] is True, "lint gates trivial work"
    write_config({"commands": {"lint": "echo lint-ok"}})
    clean = run(repo, {"task": "fix typo in README", "agents": {"implement:claude": DONE}})["result"]
    assert clean["lint"]["passed"] is True and clean["requires_human_review"] is False


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
    review = next(c for c in out["calls"] if c["label"] == "review:codex")
    finding = review["schema"]["properties"]["findings"]["items"]
    assert "category" not in finding["required"]
    assert finding["properties"]["category"] == {
        "type": "string", "enum": ["correctness", "efficiency", "best-practice", "security", "standards", "reduction"]}
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


def test_claude_usage_limit_mid_task_hands_remaining_work_to_codex(repo, write_config):
    write_config({"balance": {"mode": "off"}})
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


def test_adaptive_split_sends_some_tasks_to_codex(repo):
    engines = set()
    for i in range(8):
        out = run(repo, {"task": f"add unit tests for module {i}", "agents": {
            "scout": {"files": [], "impact": "x"}, "implement:claude": DONE,
            "review:claude": NO_FINDINGS, "review:codex": NO_FINDINGS}})
        result = out["result"]
        engines.add(result["implementation_engine"])
        assert result["review_engine"] != result["implementation_engine"]
        if result["implementation_engine"] == "codex":
            assert result["implementation_model"] == "gpt-6.1-terra"
    assert engines == {"claude", "codex"}


def test_failing_tests_get_one_fix_pass(repo, write_config):
    write_config({"commands": {"test": "test -f .fixed || { touch .fixed; echo FAIL; exit 1; }"}, "balance": {"mode": "off"}})
    out = run(repo, {"task": "add unit tests for parser", "agents": {
        "scout": {"files": ["parser.py"], "impact": "x"}, "implement:claude": [DONE, DONE], "review:codex": NO_FINDINGS, "review:style": NO_FINDINGS}})
    result = out["result"]
    assert result["tests"]["passed"] is True
    assert [e["why"] for e in result["fix_events"]] == ["fix pass on the same engine"]
    fix_prompt = [c for c in out["calls"] if c["label"] == "implement:claude"][1]["prompt"]
    assert "handoffs/" in fix_prompt
    assert result["requires_human_review"] is False


def test_scout_upgrade_runs_plan_at_new_tier(repo, write_config):
    write_config({"balance": {"mode": "off"}})
    out = run(repo, {"task": "add unit tests for parser", "agents": {
        "scout": {"files": ["parser.py"], "impact": "broad", "route": "complex", "route_reason": "multiple modules"},
        "plan": {"assessment": "a", "steps": ["s"], "risks": [], "test_strategy": "t"},
        "implement:claude": DONE, "review:codex": NO_FINDINGS}})
    assert out["result"]["route"] == "complex"
    assert out["result"]["implementation_effort"] == "high"
    assert "plan" in labels(out["calls"])
    assert "review:codex" in labels(out["calls"])
    assert out["result"]["reroute_events"] == [
        {"source": "scout", "from": "standard", "to": "complex", "reason": "multiple modules"}]


def test_scout_cannot_downgrade_critical(repo, write_config):
    write_config({"balance": {"mode": "off"}})
    out = run(repo, {"task": "Refactor the authentication service and add tests", "agents": {
        "scout": {"files": [], "impact": "small", "route": "standard", "route_reason": "small"},
        "plan": {"assessment": "a", "steps": ["s"], "risks": [], "test_strategy": "t"},
        "implement:claude": DONE, "review:codex": NO_FINDINGS}})
    assert out["result"]["route"] == "critical"
    assert out["result"]["reroute_events"] == []


def test_implementer_reroutes_with_handoff(repo, write_config):
    write_config({"balance": {"mode": "off"}})
    partial = dict(DONE, status="partial", completed_work="investigated", remaining_work="finish change",
                   reroute="up", reroute_reason="multiple modules")
    out = run(repo, {"task": "add unit tests for parser", "agents": {
        "scout": {"files": [], "impact": "x"}, "implement:claude": [partial, DONE], "review:codex": NO_FINDINGS,
        "plan": {"assessment": "a", "steps": ["split parser"], "risks": [], "test_strategy": "t"}}})
    assert labels(out["calls"]).index("plan") > labels(out["calls"]).index("implement:claude"), "upgrade gets a plan"
    calls = [c for c in out["calls"] if c["label"] == "implement:claude"]
    assert len(calls) == 2 and calls[1]["effort"] == "high" and "handoffs/" in calls[1]["prompt"]
    handoff = (repo / out["result"]["handoffs"][0]).read_text()
    assert "investigated" in handoff and "finish change" in handoff and "split parser" in handoff
    assert out["result"]["reroute_events"] == [
        {"source": "implementer", "from": "standard", "to": "complex", "reason": "multiple modules"}]
    assert out["result"]["fallback_events"] == []


def test_only_one_reroute(repo, write_config):
    write_config({"balance": {"mode": "off"}})
    partial = dict(DONE, status="partial", remaining_work="finish", reroute="up", reroute_reason="harder")
    out = run(repo, {"task": "add unit tests for parser", "agents": {
        "scout": {"files": [], "impact": "broad", "route": "complex", "route_reason": "broad"},
        "plan": {"assessment": "a", "steps": ["s"], "risks": [], "test_strategy": "t"},
        "implement:claude": partial, "review:codex": NO_FINDINGS}})
    assert out["result"]["route"] == "complex"
    assert len(out["result"]["reroute_events"]) == 1
    assert labels(out["calls"]).count("implement:claude") == 1


def test_reroute_does_not_consume_fallback_attempt(repo, write_config, monkeypatch):
    write_config({"balance": {"mode": "off"}})
    monkeypatch.setenv("FAKE_CODEX", "limit")
    partial = dict(DONE, status="partial", remaining_work="finish", reroute="down", reroute_reason="simple")
    # A one-step downgrade to trivial still routes to Claude; its later limit can use Codex.
    limited = dict(DONE, status="partial", remaining_work="finish", usage_limited=True)
    out = run(repo, {"task": "add unit tests for parser", "agents": {
        "scout": {"files": [], "impact": "x"}, "implement:claude": [partial, limited], "review:claude": NO_FINDINGS}})
    assert out["result"]["reroute_events"][0]["to"] == "trivial"
    assert out["result"]["fallback_events"][0]["to"] == "codex"


def test_resumed_handoff_skips_scout_and_plan(repo, write_config):
    write_config({"balance": {"mode": "off"}})
    handoff = repo / ".claude" / "orchestrator" / "handoffs" / "t.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("## Remaining work\nfinish")
    out = run(repo, {"task": "debug the flaky parser", "args": {"handoff": ".claude/orchestrator/handoffs/t.md"},
                     "agents": {"implement:claude": DONE, "review:codex": NO_FINDINGS}})
    assert "scout" not in labels(out["calls"]) and "plan" not in labels(out["calls"])


OR_MODEL = "openai/gpt-6.1-terra"
RUNS = (".claude", "orchestrator", "openrouter-runs")


@pytest.fixture
def openrouter(repo, write_config, fake_openrouter, monkeypatch):
    """Enable OpenRouter with gpt-6.1-terra on it; the runner reaches the fake key server."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_POLL_SECONDS", "0.2")

    def configure(**extra):
        write_config({"engines": {"openrouter": True}, "balance": {"mode": "off"},
                      "models": {"gpt-6.1-terra": {"engines": {"openrouter": OR_MODEL}}}, **extra})
    return configure


def test_fallback_claude_to_openrouter_when_codex_limited(repo, openrouter):
    openrouter()
    set_state(repo, codex="limited")
    limited = dict(DONE, status="partial", completed_work="Wrote parser tests for dates",
                   remaining_work="Write parser tests for numbers", usage_limited=True)
    out = run(repo, {"task": "add unit tests for parser", "agents": {
        "scout": {"files": ["parser.py"], "impact": "small"}, "implement:claude": limited}})
    result = out["result"]
    assert result["fallback_events"][0]["from"] == "claude" and result["fallback_events"][0]["to"] == "openrouter"
    assert (result["implementation_engine"], result["implementation_model"]) == ("openrouter", OR_MODEL)
    assert result["files_changed"] == ["app.py"]
    assert state(repo)["claude"]["status"] == "limited"
    handoff = (repo / result["fallback_events"][0]["handoff"]).read_text()
    assert "## Replacement engine\nopenrouter" in handoff and "## Remaining work\nWrite parser tests for numbers" in handoff


def test_openrouter_pause_stops_for_human(repo, openrouter, fake_openrouter, monkeypatch):
    openrouter(openrouter={"budget": {"daily_usd": 1}})
    set_state(repo, claude="limited", codex="limited")
    monkeypatch.setenv("FAKE_SLEEP", "5")
    runs = repo.joinpath(*RUNS)

    def overspend():  # once Codex runs, the next spend poll crosses the daily cap
        deadline = time.monotonic() + 30
        while not list(runs.glob("*/pid")) and time.monotonic() < deadline:
            time.sleep(0.05)
        fake_openrouter.key_data["usage_daily"] = 5

    threading.Thread(target=overspend, daemon=True).start()
    out = run(repo, {"task": "add unit tests for parser", "agents": {}})
    run_id = next(runs.iterdir()).name
    subprocess.run([sys.executable, str(ROOT / "scripts" / "openrouter_run.py"), "--stop", run_id], capture_output=True)
    result = out["result"]
    assert result["requires_human_review"] is True
    assert result["summary"] == (f"OpenRouter paused over its daily budget: run openrouter_run.py --resume {run_id}"
                                 f" or --stop {run_id}.")
    assert not [c for c in out["calls"] if c["kind"] == "agent"]


SUPERCRITICAL = {"route": "supercritical", "reason": "test"}
PLAN = {"assessment": "a", "steps": ["s"], "risks": [], "test_strategy": "t"}


def test_supercritical_headless_auto_uses_medium(repo):
    out = run(repo, {"task": "migrate the billing ledger", "args": {"classification": SUPERCRITICAL, "permission_mode": "auto"},
                     "agents": {"scout": {"files": [], "impact": "x"}, "plan": PLAN, "implement:claude": DONE,
                                "review:claude": NO_FINDINGS, "review:style": NO_FINDINGS}})
    calls = out["calls"]
    plan = next(c for c in calls if c["label"] == "plan")
    assert (plan["model"], plan["effort"]) == ("claude-fable-5-2", "medium")
    assert next(c for c in calls if c["label"] == "implement:claude")["model"] == "opus"
    review = next(c for c in calls if c["label"] == "review:claude")
    assert (review["model"], review["effort"]) == ("claude-fable-5-2", "medium")
    assert out["result"]["route"] == "supercritical"


def test_supercritical_auto_inherits_parent_effort(repo):
    subprocess.run([sys.executable, str(ROOT / "scripts" / "engine_state.py"), "set-effort", "--task-id", "parent-1",
                    "--effort", "high"], check=True, capture_output=True)
    out = run(repo, {"task": "migrate the billing ledger", "args": {
        "classification": SUPERCRITICAL, "permission_mode": "auto", "parent_task_id": "parent-1"},
        "agents": {"scout": {"files": [], "impact": "x"}, "plan": PLAN, "implement:claude": DONE, "review:claude": NO_FINDINGS}})
    assert next(c for c in out["calls"] if c["label"] == "plan")["effort"] == "high"


def test_supercritical_headless_manual_stops_before_top_model(repo):
    out = run(repo, {"task": "migrate the billing ledger", "args": {"classification": SUPERCRITICAL},
                     "agents": {"scout": {"files": [], "impact": "x"}}})
    assert "plan" not in labels(out["calls"]) and "implement:claude" not in labels(out["calls"])
    result = out["result"]
    assert result["requires_human_review"] is True and result["implementation_engine"] == "none"
    tid = result["task_id"]
    assert f"set-effort --task-id {tid} --effort <level>, then rerun with task_id: {tid}." in result["summary"]


def test_supercritical_fix_escalation_never_uses_top_model(repo, write_config):
    write_config({"commands": {"test": "echo FAIL; exit 1"}})
    out = run(repo, {"task": "migrate the billing ledger", "args": {"classification": SUPERCRITICAL, "permission_mode": "auto"},
                     "agents": {"scout": {"files": [], "impact": "x"}, "plan": PLAN, "implement:claude": [DONE, DONE],
                                "review:claude": NO_FINDINGS, "review:style": NO_FINDINGS}})
    impls = [c for c in out["calls"] if c["label"] == "implement:claude"]
    assert len(impls) == 2 and all(c["model"] == "opus" for c in impls), "the top model never implements"
    assert [e["why"] for e in out["result"]["fix_events"]] == ["fix pass on the same engine"]


def test_unavailable_architecture_skips_plan(repo, write_config):
    write_config({"balance": {"mode": "off"},
                  "routing": {"architecture": {"complex": {"models": ["gpt-6-astra"], "effort": "high"}}}})
    set_state(repo, codex="limited")
    out = run(repo, {"task": "add unit tests for parser", "args": {"classification": {"route": "complex", "reason": "test"}},
                     "agents": {"scout": {"files": [], "impact": "x"}, "implement:claude": DONE, "review:claude": NO_FINDINGS}})
    assert "plan" not in labels(out["calls"]) and "implement:claude" in labels(out["calls"])
    assert out["result"]["implementation_engine"] == "claude"


def test_long_task_id_is_replaced(repo):
    out = run(repo, {"task": "fix typo in README", "args": {"task_id": "a" * 65},
                     "agents": {"implement:claude": DONE, "review:claude": NO_FINDINGS}})
    assert out["result"]["task_id"].startswith("fix-typo-in-readme-")
