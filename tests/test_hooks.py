import json
import os
import subprocess
import sys

from conftest import ROOT

HOOKS = ROOT / "hooks" / "scripts"


def run_hook(name, payload, project):
    out = subprocess.run(
        [sys.executable, str(HOOKS / name)],
        input=json.dumps(payload), capture_output=True, text=True, check=True, timeout=5,
        env={"CLAUDE_PROJECT_DIR": str(project), "PATH": "/usr/bin:/bin",
             **{k: os.environ[k] for k in ("ORCHESTRATOR_CLAUDE_USAGE_FILE", "CODEX_HOME")}},
    )
    return json.loads(out.stdout) if out.stdout.strip() else None


def context(project, prompt="Refactor the authentication service and add tests"):
    out = run_hook("inject_orchestration_context.py", {"prompt": prompt, "cwd": str(project)}, project)
    assert set(out) == {"hookSpecificOutput"}
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    return out["hookSpecificOutput"]["additionalContext"]


def test_injects_policy_and_offers_config(project):
    text = context(project)
    assert text.startswith("ORCHESTRATION POLICY ACTIVE")
    assert "claude=healthy(100%)" in text
    assert "Create .claude/orchestrator.config.json with default orchestration settings?" in text


def test_injects_same_policy_for_trivial_prompts(project, write_config):
    write_config({})
    text = context(project, "What does this function do?")
    assert "THIS PROMPT: question." in text
    assert "Create .claude" not in text


def test_disabled(project, write_config):
    write_config({"engines": {"claude": False, "codex": False}, "automatic": False})
    assert context(project) == "ORCHESTRATION POLICY DISABLED"


def test_invalid_config_does_not_crash(project, write_config):
    write_config("{oops")
    assert context(project).startswith("ORCHESTRATION CONFIG INVALID")


def test_reports_engine_state(project):
    (project / ".claude" / "engine-state.json").write_text(json.dumps({"codex": {"status": "limited", "remaining_pct": 0}}))
    assert "codex=limited(0%)" in context(project)


def dispatch(project, agent, prompt):
    payload = {"tool_name": "Agent", "cwd": str(project), "tool_input": {"subagent_type": agent, "prompt": prompt, "description": "x"}}
    out = run_hook("validate_agent_dispatch.py", payload, project)
    if out is None:
        return "allow", ""
    hso = out["hookSpecificOutput"]
    return hso["permissionDecision"], hso["permissionDecisionReason"]


def test_architect_dispatch_uses_model_classification(project):
    assert dispatch(project, "architect", "fix typo in README")[0] == "allow"


def test_allows_complex_task_to_architect(project):
    assert dispatch(project, "cost-aware-orchestrator:architect", "debug intermittent failure across services")[0] == "allow"


def test_engine_limits_block_matching_agents(project):
    state = project / ".claude" / "engine-state.json"
    state.write_text(json.dumps({"claude": {"status": "limited"}}))
    assert dispatch(project, "cost-aware-orchestrator:implementer", "add tests")[0] == "deny"
    state.write_text(json.dumps({"codex": {"status": "limited"}}))
    assert dispatch(project, "cost-aware-orchestrator:codex-reviewer", "review diff")[0] == "deny"
    assert dispatch(project, "cost-aware-orchestrator:implementer", "add tests")[0] == "allow"


def test_style_reviewer_is_allowed(project):
    assert dispatch(project, "cost-aware-orchestrator:style-reviewer", "review diff")[0] == "allow"
    assert dispatch(project, "style-reviewer", "review diff")[0] == "allow"


def test_ignores_other_agents_and_disabled_policy(project, write_config):
    assert dispatch(project, "other-plugin:architect", "fix typo")[0] == "allow"
    assert dispatch(project, "Explore", "fix typo")[0] == "allow"
    write_config({"automatic": False})
    assert dispatch(project, "architect", "fix typo")[0] == "allow"


def test_directive_rows_rubric_and_floor(project, write_config):
    write_config({"commands": {"test": "pytest -q"}, "balance": {"mode": "off"}})
    text = context(project, "add a button")
    for tier in ("trivial", "standard", "complex", "critical"):
        assert f"\n{tier}:" in text
    assert "Rubric: trivial =" in text and "irreversible actions" in text
    assert "whole conversation" in text and "`route=<tier>: <reason>`" in text
    assert "inherits the tier of what it approves, never lower" in text
    assert text.count("Agents are cost-aware-orchestrator:<name>.") == 1
    assert "Floor:" not in text
    assert "Floor: at least critical" in context(project, "drop the users table")
    assert "Floor: at least complex" in context(project, "debug intermittent failure")
    critical = context(project, "drop the users table")
    assert "\ncritical:" in critical
    assert all(f"\n{tier}:" not in critical for tier in ("trivial", "standard", "complex"))
    complex_text = context(project, "debug intermittent failure")
    assert all(f"\n{tier}:" in complex_text for tier in ("complex", "critical"))
    assert all(f"\n{tier}:" not in complex_text for tier in ("trivial", "standard"))
    assert "review=codex-reviewer/" in next(line for line in text.splitlines() if line.startswith("complex:"))
    assert text.count("Codex: write_handoff.py") == 1
    for prompt in ("yes", "fix typo in README", "commit and push"):
        assert "Floor:" not in context(project, prompt)
    assert "pytest -q" in text and "Visibility:" in text


def test_questions_skip_routing(project):
    for prompt in ("Explain the architecture", "How does the router pick an engine?"):
        text = context(project, prompt)
        assert "THIS PROMPT: question. Answer it directly" in text
        assert "Rubric:" not in text


def test_directive_routes_to_codex_when_claude_limited(project):
    (project / ".claude" / "engine-state.json").write_text(json.dumps({"claude": {"status": "limited", "remaining_pct": 0}}))
    text = context(project, "add unit tests for parser")
    assert "Rows marked [Codex] implement on Codex: delegate" in text and "codex_with_fallback.sh" in text
    assert "trivial: handle directly, lowest effort, no review." in text


def test_directive_no_engine_and_constrained_quota(project, write_config):
    write_config({"engines": {"claude": False, "codex": False}})
    text = context(project, "add a button")
    assert "No engine available" in text and "do not start; ask user" in text
    write_config({})
    (project / ".claude" / "engine-state.json").write_text(json.dumps({
        "claude": {"status": "near_limit", "remaining_pct": 10},
        "codex": {"status": "near_limit", "remaining_pct": 10},
    }))
    text = context(project, "add a button")
    assert "quota constrained: one atomic task, then human review" in text


def test_hook_is_fast(project):
    import time
    start = time.monotonic()
    context(project, "Refactor the authentication service and add tests")
    assert time.monotonic() - start < 2


def test_subagent_handback_is_ignored(project):
    handback = '<agent-message from="a1">\n[Subagent hand-back] Changed auth credentials handling; no secrets stored.'
    assert run_hook("inject_orchestration_context.py", {"prompt": handback}, project) is None


def test_observed_usage_changes_routing(project, usage_files):
    from test_usage_sync import write_claude
    write_claude(usage_files, five=85)
    text = context(project, "add unit tests for parser")
    assert "claude=near_limit(15%)" in text
    assert "implement=[Codex] Codex/" in text


def test_task_keys_include_prompt_and_session(monkeypatch, project):
    import importlib.util
    spec = importlib.util.spec_from_file_location("inject_context", HOOKS / "inject_orchestration_context.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keys = []
    original_route = module.route

    def capture_route(*args, **kwargs):
        if "task_key" in kwargs:
            keys.append(kwargs["task_key"])
        return original_route(*args, **kwargs)

    monkeypatch.setattr(module, "route", capture_route)
    config = module.load_config(project)
    state = module.load_synced_state(config, project)
    first = module.directive("add a button", config, state, session_id="one")
    assert len(first) < 1800
    module.directive("add a button", config, state, session_id="one")
    module.directive("add a button", config, state, session_id="two")
    module.directive("add a menu", config, state, session_id="one")
    assert len(set(keys[:3])) == 1 and keys[0] == keys[3] and keys[0] != keys[6] != keys[9]
    assert len(module.directive("drop the users table", config, state, session_id="one")) < 1450
