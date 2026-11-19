import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import ROOT

HOOKS = ROOT / "hooks" / "scripts"


FORWARDED = ("ORCHESTRATOR_CLAUDE_USAGE_FILE", "CODEX_HOME", "OPENROUTER_API_KEY",
             "ORCHESTRATOR_OPENROUTER_USAGE_FILE", "ORCHESTRATOR_OPENROUTER_MODELS_FILE", "ORCHESTRATOR_DEFAULT_CONFIG")


def run_hook(name, payload, project):
    out = subprocess.run(
        [sys.executable, str(HOOKS / name)],
        input=json.dumps(payload), capture_output=True, text=True, check=True, timeout=5,
        env={"CLAUDE_PROJECT_DIR": str(project), "PATH": "/usr/bin:/bin",
             **{k: os.environ[k] for k in FORWARDED if k in os.environ}},
    )
    return json.loads(out.stdout) if out.stdout.strip() else None


def context(project, prompt="Refactor the authentication service and add tests", **extra):
    out = run_hook("inject_orchestration_context.py", {"prompt": prompt, "cwd": str(project), **extra}, project)
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
    decision, reason = dispatch(project, "cost-aware-orchestrator:implementer", "add tests")
    assert decision == "deny" and "another engine" in reason and "to Codex" not in reason
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
    for tier in ("standard", "complex", "critical", "supercritical"):
        assert next(line for line in text.splitlines() if line.startswith(f"{tier}:")).endswith(
            "; docs=doc-updater/haiku/low."), tier
    assert "docs=" not in next(line for line in text.splitlines() if line.startswith("trivial:"))
    assert text.count("Codex: write_handoff.py") == 1
    for prompt in ("yes", "fix typo in README", "commit and push"):
        assert "Floor:" not in context(project, prompt)
    assert "pytest -q" in text and "Visibility:" in text


def test_directive_shows_only_different_type_rows(project, write_config):
    default = context(project, "add a button")
    assert "Types (only if clearly one)" not in default and "/frontend:" not in default
    write_config({"routing": {"implementation": {"complex": {"types": {"frontend": {
        "models": ["sonnet"], "effort": "high"}}}}}})
    typed = context(project, "add a button")
    assert typed.count("Types (only if clearly one): frontend, debug, refactor, tests, docs:") == 1
    lines = typed.splitlines()
    tier = next(line for line in lines if line.startswith("complex:"))
    frontend = next(line for line in lines if line.startswith("complex/frontend:"))
    assert "implement=implementer/sonnet/high" in frontend
    # Only entries that differ from the tier row are listed (a new writer may change the reviewer).
    tier_entries = set(tier.split(": ", 1)[1].rstrip(".").split("; "))
    for entry in frontend.split(": ", 1)[1].rstrip(".").split("; "):
        assert entry not in tier_entries
    assert "style=" not in frontend and "docs=" not in frontend


def test_directive_omits_type_instruction_when_no_type_lines_are_shown(project, write_config):
    type_row = {"frontend": {"models": ["haiku"], "effort": "low"}}
    for tier, prompt in (("standard", "drop the users table"), ("trivial", "add a button")):
        write_config({"routing": {"implementation": {tier: {"types": type_row}}}})
        text = context(project, prompt)
        assert "Types (only if clearly one)" not in text
        assert "/frontend:" not in text


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


def test_directive_shows_resolved_codex_model(project, usage_files):
    (usage_files / "codex").mkdir()
    (usage_files / "codex" / "models_cache.json").write_text(json.dumps({
        "models": [{"slug": "gpt-5.6-terra", "visibility": "list"}],
    }))
    (project / ".claude" / "engine-state.json").write_text(
        json.dumps({"claude": {"status": "limited", "remaining_pct": 0}})
    )
    text = context(project, "add unit tests for parser")
    assert "implement=[Codex] Codex/gpt-5.6-terra/medium" in text


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


def test_type_rows_without_engine_are_one_line_per_tier(project, write_config):
    write_config({"engines": {"codex": False}, "routing": {"implementation": {"complex": {"types": {"debug": {
        "models": ["gpt-6.1-terra"], "effort": "high"}}}}}})
    lines = context(project, "add a button").splitlines()
    typed = [line for line in lines if "/debug:" in line or "/frontend:" in line]
    assert len(typed) == 1 and typed[0].startswith("complex/debug: No engine available (")
    assert typed[0].endswith("); do not start; ask user.")


def test_type_rows_compare_by_step_key(project, write_config):
    write_config({"routing": {"implementation": {"standard": {"types": {"tests": {
        "models": ["haiku"], "effort": "low"}}}}}})
    (project / ".claude" / "engine-state.json").write_text(json.dumps({
        "claude": {"status": "near_limit", "remaining_pct": 10},
        "codex": {"status": "near_limit", "remaining_pct": 10},
    }))
    lines = context(project, "add a button").splitlines()
    base = next(line for line in lines if line.startswith("standard:"))
    typed = next(line for line in lines if line.startswith("standard/tests:"))
    # The base row is quota constrained and the type row is not: no misaligned docs= or quota entry.
    assert "quota constrained" in base
    assert typed == "standard/tests: implement=implementer/haiku/low; review=verifier/sonnet/medium."
    assert not any(line.startswith(("complex/", "critical/", "standard/docs")) for line in lines)


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
        if args[0] == "implementation" and "task_key" in kwargs:
            keys.append(kwargs["task_key"])
        return original_route(*args, **kwargs)

    monkeypatch.setattr(module, "route", capture_route)
    config = module.load_config(project)
    state = module.load_synced_state(config, project)
    first = module.directive("add a button", config, state, session_id="one")
    assert len(first) < 2400
    module.directive("add a button", config, state, session_id="one")
    module.directive("add a button", config, state, session_id="two")
    module.directive("add a menu", config, state, session_id="one")
    assert len(set(keys[:4])) == 1 and keys[0] == keys[4] and keys[0] != keys[8] != keys[12]
    assert len(module.directive("drop the users table", config, state, session_id="one")) < 2100


def test_openrouter_hidden_when_disabled(project, write_config):
    write_config({})
    text = context(project, "add a button")
    assert "openrouter=" not in text and "OpenRouter" not in text


def test_openrouter_shown_when_enabled(project, write_config, monkeypatch):
    write_config({"engines": {"openrouter": True}})
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    text = context(project, "add a button")
    assert "openrouter=healthy(100%)" in text


def test_openrouter_without_budget_gets_a_notice(project, write_config, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    write_config({"engines": {"openrouter": True}})
    assert "OpenRouter has no monthly budget" in context(project, "add a button")
    write_config({"engines": {"openrouter": True}, "openrouter": {"budget": {"monthly_usd": 20}}})
    assert "OpenRouter has no monthly budget" not in context(project, "add a button")


def test_supercritical_row_and_effort_line(project, write_config, monkeypatch):
    write_config({"engines": {"openrouter": True}})
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    text = context(project, "add a button", permission_mode="auto")
    assert "supercritical = needs frontier reasoning" in text
    row = next(line for line in text.splitlines() if line.startswith("supercritical:"))
    assert "architect required=architect/" in row and "/medium" in row and "review=" in row
    assert "user-named effort: engine_state.py set-effort --task-id" in text
    floor = context(project, "drop the users table")
    assert "\nsupercritical:" in floor and "Floor: at least critical" in floor
    write_config({"routing": {role: {"supercritical": {"models": ["fable", "gpt-6-astra"], "effort": "ask"}}
                                   for role in ("review", "architecture")}})
    assert "/ASK" in next(line for line in context(project, "add a button").splitlines()
                          if line.startswith("supercritical:"))


def test_legacy_keys_notice(project, write_config):
    write_config({"defaults": {}})
    text = context(project, "add a button")
    assert 'Legacy config keys ignored: defaults; models now come from "models"/"routing"' in text


def test_guard_denies_openrouter_review_when_openrouter_limited(project):
    (project / ".claude" / "engine-state.json").write_text(json.dumps({"openrouter": {"status": "limited"}}))
    assert dispatch(project, "cost-aware-orchestrator:codex-reviewer", "run openrouter_run.py handoff")[0] == "deny"
    assert dispatch(project, "cost-aware-orchestrator:codex-reviewer", "review diff")[0] == "allow"


def openrouter_only(write_config, monkeypatch, role):
    """Config where the standard row for ``role`` can only run on OpenRouter."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    write_config({
        "engines": {"codex": False, "openrouter": True},
        "balance": {"mode": "off"},
        "models": {"or-model": {"score": 99, "engines": {"openrouter": "vendor/model"}}},
        "routing": {role: {"standard": {"models": ["or-model"], "effort": "medium"}}},
    })


def test_openrouter_review_row_and_run_line(project, write_config, monkeypatch):
    openrouter_only(write_config, monkeypatch, "review")
    text = context(project, "add a button")
    assert "codex-reviewer[OpenRouter]/vendor/model/medium" in text
    assert "openrouter_run.py <handoff>; exit 43" in text
    run_line = next(line for line in text.splitlines() if line.startswith("OpenRouter: "))
    assert "OPENROUTER_SANDBOX=workspace-write (read-only for scout/style)" in run_line
    assert "codex_with_fallback.sh" not in text and "Rows marked [OpenRouter]" not in text


def test_openrouter_implementation_row_delegates(project, write_config, monkeypatch):
    openrouter_only(write_config, monkeypatch, "implementation")
    text = context(project, "add a button")
    assert "implement=[OpenRouter] OpenRouter/vendor/model/medium" in text
    assert "Rows marked [OpenRouter] implement on OpenRouter: delegate" in text
    assert "openrouter_run.py <handoff>" in text


def test_decider_line_replaces_classify_line_only_when_usable(project, write_config, monkeypatch):
    from test_decide_route import enable_decider
    enable_decider(write_config, monkeypatch)
    text = context(project, "add a button")
    assert "decide_route.py" in text and "Classify this prompt using" not in text
    assert "Rubric: trivial =" in text
    decider_line = next(line for line in text.splitlines() if "decide_route.py" in line)
    assert "its route is a floor, raise it from context" in decider_line and "Approvals (yes) keep" in decider_line
    assert "ask:true" in decider_line and "AskUserQuestion" in decider_line and "headless: higher" in decider_line
    assert "follow its row" in decider_line
    # The model never quotes user text into a shell: the prompt goes through a file.
    assert "with Write" in decider_line and "--prompt-file" in decider_line and "stdin" not in decider_line
    directive = "\n".join(text.splitlines()[text.splitlines().index(decider_line):])
    # Owner-set budget for decider mode (default mode keeps 2400); Claude Code's own limit is 10000.
    assert len(directive) < 5000, "the decider-mode hook output is over its budget"
    (project / ".claude" / "engine-state.json").write_text(json.dumps({"openrouter": {"status": "limited"}}))
    text = context(project, "add a button")
    assert "decide_route.py" not in text


def test_directive_research_only_when_configured(project):
    text = context(project, "add a button")
    assert text.count("needs_research") == 1
    assert "research=" in next(line for line in text.splitlines() if line.startswith("standard:"))
    assert "research=" not in next(line for line in text.splitlines() if line.startswith("trivial:"))
    path = Path(os.environ["ORCHESTRATOR_DEFAULT_CONFIG"])
    config = json.loads(path.read_text())
    del config["routing"]["research"]
    path.write_text(json.dumps(config))
    assert "research" not in context(project, "add a button").lower()
