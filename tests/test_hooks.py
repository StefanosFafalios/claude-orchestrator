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
    assert "Do not run the full workflow for trivial conversational requests." in text
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


def test_denies_trivial_task_to_architect(project):
    decision, reason = dispatch(project, "cost-aware-orchestrator:architect", "fix typo in README")
    assert decision == "deny" and "Trivial" in reason


def test_standard_task_to_architect_needs_justification(project):
    assert dispatch(project, "architect", "add unit tests for parser")[0] == "deny"
    justified = "add unit tests for parser\nJustification: the parser has undocumented invariants"
    assert dispatch(project, "architect", justified)[0] == "allow"


def test_allows_complex_task_to_architect(project):
    assert dispatch(project, "cost-aware-orchestrator:architect", "debug intermittent failure across services")[0] == "allow"


def test_engine_limits_block_matching_agents(project):
    state = project / ".claude" / "engine-state.json"
    state.write_text(json.dumps({"claude": {"status": "limited"}}))
    assert dispatch(project, "cost-aware-orchestrator:implementer", "add tests")[0] == "deny"
    state.write_text(json.dumps({"codex": {"status": "limited"}}))
    assert dispatch(project, "cost-aware-orchestrator:codex-reviewer", "review diff")[0] == "deny"
    assert dispatch(project, "cost-aware-orchestrator:implementer", "add tests")[0] == "allow"


def test_ignores_other_agents_and_disabled_policy(project, write_config):
    assert dispatch(project, "other-plugin:architect", "fix typo")[0] == "allow"
    assert dispatch(project, "Explore", "fix typo")[0] == "allow"
    write_config({"automatic": False})
    assert dispatch(project, "architect", "fix typo")[0] == "allow"


def test_directive_for_standard_prompt(project, write_config):
    write_config({"commands": {"test": "pytest -q"}, "balance": {"mode": "off"}})
    text = context(project, "Add a subtract function to calc.py and add tests for it.")
    assert "THIS PROMPT: route=standard" in text
    assert "implement with the `cost-aware-orchestrator:implementer` agent (model sonnet" in text
    assert "pytest -q" in text and "one fix pass" in text
    assert "codex-reviewer" in text, "claude-implemented work is reviewed on codex"


def test_directive_for_trivial_prompt_avoids_fan_out(project):
    text = context(project, "What does this function do?")
    assert "route=trivial" in text and "no subagents" in text


def test_directive_for_critical_prompt_plans_and_reviews_on_codex(project):
    text = context(project, "Refactor the authentication service and add tests")
    assert "route=critical" in text
    assert "scout" in text and "architect" in text and "codex-reviewer" in text
    assert "if the scout reports a different route, rerun route_engine.py once" in text


def test_directive_routes_to_codex_when_claude_limited(project):
    (project / ".claude" / "engine-state.json").write_text(json.dumps({"claude": {"status": "limited", "remaining_pct": 0}}))
    text = context(project, "add unit tests for parser")
    assert "implement on Codex" in text and "codex_with_fallback.sh" in text


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
    assert "implement on Codex" in text


def test_directive_split_sends_some_standard_prompts_to_codex(project):
    prompts = [f"add unit tests for module {i}" for i in range(20)]
    texts = [context(project, p) for p in prompts]
    to_codex = [t for t in texts if "implement on Codex" in t]
    assert 0 < len(to_codex) < 20
    for text in to_codex:
        assert "REQUIRED (adaptive split" in text and "CODEX_EFFORT=medium" in text
        assert "verifier" in text, "codex-implemented work is reviewed on claude"
