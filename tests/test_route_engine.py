import json

import pytest
from conftest import TEMPLATE

from engine_state import load_config
from model_map import engine_name, row, score
from route_engine import route


def state(claude="healthy", codex="healthy", claude_pct=100, codex_pct=100, openrouter="healthy", openrouter_pct=100):
    def entry(status, pct):
        return {"status": status, "remaining_pct": pct, "last_error": None, "last_checked": None}
    return {"claude": entry(claude, claude_pct), "codex": entry(codex, codex_pct),
            "openrouter": entry(openrouter, openrouter_pct)}


def write_models(usage_files, *slugs):
    home = usage_files / "codex"
    home.mkdir()
    (home / "models_cache.json").write_text(
        __import__("json").dumps({"models": [{"slug": slug, "visibility": "list"} for slug in slugs]})
    )


@pytest.fixture
def config(project):
    return load_config()


def enable_openrouter(config, monkeypatch):
    """Turn OpenRouter on with its own model on the standard implementation row."""
    config["models"]["or-model"] = {"score": 60, "engines": {"openrouter": "vendor/or-model"}}
    config["engines"]["openrouter"] = True
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    config["routing"]["implementation"]["standard"]["models"].append("or-model")


@pytest.mark.parametrize("task_route, model, effort", [
    ("trivial", "haiku", "low"),
    ("standard", "sonnet", "medium"),
    ("complex", "sonnet", "high"),
    ("critical", "opus", "high"),
    ("supercritical", "opus", "high"),
])
def test_healthy_implementation_tiers(config, task_route, model, effort):
    result = route("implementation", task_route, config, state())
    assert (result["engine"], result["model"], result["effort"]) == ("claude", model, effort)
    assert result["requires_human_review"] is False


def test_claude_limited(config):
    s = state(claude="limited", claude_pct=0)
    assert route("implementation", "standard", config, s)["engine"] == "codex"
    assert route("review", "standard", config, s)["engine"] == "codex"


def test_codex_limited(config):
    s = state(codex="limited", codex_pct=0)
    impl = route("implementation", "critical", config, s)
    assert (impl["engine"], impl["model"]) == ("claude", "opus")
    review = route("review", "critical", config, s)
    assert (review["engine"], review["model"], review["effort"]) == ("claude", "opus", "high")


def test_claude_near_limit(config):
    assert route("implementation", "standard", config, state(claude="near_limit", claude_pct=20))["engine"] == "codex"
    both = state(claude="near_limit", codex="near_limit", claude_pct=20, codex_pct=10)
    assert route("review", "standard", config, both)["engine"] == "claude"


def test_codex_near_limit(config):
    impl = route("implementation", "standard", config, state(codex="near_limit", codex_pct=15))
    assert (impl["engine"], impl["model"]) == ("claude", "sonnet")


@pytest.mark.parametrize("claude_pct, codex_pct, expected", [(20, 10, "claude"), (5, 22, "codex")])
def test_both_near_limit(config, claude_pct, codex_pct, expected):
    s = state(claude="near_limit", codex="near_limit", claude_pct=claude_pct, codex_pct=codex_pct)
    for role in ("implementation", "review"):
        result = route(role, "standard", config, s)
        assert result["engine"] == expected
        assert result["requires_human_review"] is True
    assert route("implementation", "standard", config, s)["scope"] == "single_atomic_task"


def test_both_limited_returns_none(config):
    result = route("implementation", "standard", config, state("limited", "limited", 0, 0))
    assert result["engine"] == "none"
    assert result["requires_human_review"] is True


def test_reviewer_never_same_model(config):
    r = route("review", "standard", config, state(), implementation_model="sonnet")
    assert r["canonical_model"] == "gpt-6.1-terra" and r["engine"] == "codex"


def test_reviewer_never_weaker_than_implementer(config):
    # complex row [sol 82, opus 85, sonnet 70, luna 55]: nothing >= 85 except the implementer itself
    r = route("review", "complex", config, state(), implementation_model="opus")
    assert r["canonical_model"] == "gpt-6.1-sol" and r["requires_human_review"] is True
    assert "no reviewer at least as capable" in r["reason"]
    stronger = route("review", "complex", config, state(), implementation_model="gpt-6.1-sol")
    assert stronger["canonical_model"] == "opus" and stronger["requires_human_review"] is False


def test_trivial_tasks_get_no_review_or_scout(config):
    for role in ("review", "style_review", "scout"):
        r = route(role, "trivial", config, state(), implementation_model="haiku")
        assert (r["engine"], r["requires_human_review"]) == ("none", False), role


@pytest.mark.parametrize("only", ["claude", "codex"])
@pytest.mark.parametrize("task_route", ["standard", "complex", "critical", "supercritical"])
def test_shipped_review_rows_cover_every_implementer(write_config, monkeypatch, only, task_route):
    """Cheapest-first review rows still hold a stronger, different reviewer on a single engine.

    The one exception is Opus on Claude alone below supercritical: only the top model outranks
    it, and top models review only supercritical work (their effort is asked), so a weaker,
    different model reviews and a human checks.
    """
    monkeypatch.setenv("ORCHESTRATOR_DEFAULT_CONFIG", str(TEMPLATE))
    write_config({"engines": {"claude": only == "claude", "codex": only == "codex"}})
    config = load_config()
    for model in row(config, "implementation", task_route)["models"]:
        if not engine_name(config, model, only):
            continue
        r = route("review", task_route, config, state(), implementation_model=model)
        assert r["engine"] == only and r["canonical_model"] != model, (model, r)
        if (only, model) == ("claude", "opus") and task_route != "supercritical":
            assert r["requires_human_review"] is True, (model, r)
            continue
        assert r["requires_human_review"] is False and score(config, r["canonical_model"]) >= score(config, model), (model, r)


def test_codex_model_missing_from_account_is_served_by_openrouter(write_config, monkeypatch, usage_files):
    monkeypatch.setenv("ORCHESTRATOR_DEFAULT_CONFIG", str(TEMPLATE))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    write_models(usage_files, "gpt-6-luna")  # the Codex account lacks gpt-6.1-terra
    write_config({"engines": {"openrouter": True}, "balance": {"mode": "off"}})
    result = route("implementation", "standard", load_config(), state())
    assert (result["engine"], result["model"], result["canonical_model"]) == ("openrouter", "openai/gpt-6.1-terra", "gpt-6.1-terra")


def test_shipped_top_models_only_in_supercritical_rows():
    routing = json.loads(TEMPLATE.read_text())["routing"]
    for role, rows in routing.items():
        for task_route, entry in rows.items():
            if (role, task_route) in (("review", "supercritical"), ("architecture", "supercritical")):
                assert entry["models"] == ["fable", "gpt-6.2-astra"]
            else:
                assert "fable" not in entry["models"], (role, task_route)


def test_review_defaults_by_route(config):
    complex_ = route("review", "complex", config, state(), implementation_model="sonnet")
    assert complex_["canonical_model"] == "gpt-6.1-sol" and complex_["requires_human_review"] is False
    critical = route("review", "critical", config, state(), implementation_model="opus")
    assert (critical["canonical_model"], critical["engine"], critical["requires_human_review"]) == (
        "gpt-6-astra", "codex", False)


def test_review_falls_back_to_weaker_model_with_human_review(config, write_config):
    write_config({"engines": {"codex": False}})
    cfg = load_config()
    critical = route("review", "critical", cfg, state(), implementation_model="opus")
    assert (critical["canonical_model"], critical["engine"], critical["requires_human_review"]) == (
        "sonnet", "claude", True)
    standard = route("review", "standard", cfg, state(), implementation_model="sonnet")
    assert (standard["canonical_model"], standard["engine"], standard["requires_human_review"]) == (
        "opus", "claude", False)


def test_low_remaining_pct_counts_as_near_limit(config):
    assert route("implementation", "standard", config, state(claude_pct=10))["engine"] == "codex"


def test_same_model_across_engines_is_skipped(config, monkeypatch):
    config["models"]["sonnet"]["engines"]["openrouter"] = "anthropic/claude-sonnet-x"
    config["engines"]["openrouter"] = True
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    r = route("review", "standard", config, state(), implementation_model="anthropic/claude-sonnet-x")
    assert r["canonical_model"] != "sonnet"


def test_review_auto_derives_implementer(config):
    for i in range(20):
        impl = route("implementation", "standard", config, state(), task_key=f"t{i}")
        r = route("review", "standard", config, state(), task_key=f"t{i}", implementation_model="auto")
        assert r["canonical_model"] != impl["canonical_model"]


def test_style_review_routing(config, write_config):
    style = route("style_review", "standard", config, state())
    assert (style["engine"], style["model"], style["effort"]) == ("claude", "haiku", "low")
    assert route("style_review", "trivial", config, state())["engine"] == "none"
    skipped = route("style_review", "standard", config, state(claude="limited"))
    assert (skipped["engine"], skipped["reason"]) == ("none", "both engines constrained; main review covers style")
    assert route("style_review", "standard", config, state(claude_pct=5))["engine"] == "none"
    assert skipped["requires_human_review"] is False
    write_config({"review": {"split": False}})
    unsplit = route("style_review", "standard", load_config(), state())
    assert (unsplit["engine"], unsplit["reason"]) == ("none", "covered by the main review")


def test_docs_role_cli(project, capsys):
    from route_engine import main
    assert main(["--role", "docs", "--task-route", "standard"]) == 0
    docs = json.loads(capsys.readouterr().out)
    assert (docs["engine"], docs["model"], docs["effort"]) == ("claude", "haiku", "low")
    assert main(["--role", "docs", "--task-route", "trivial"]) == 0
    assert json.loads(capsys.readouterr().out)["engine"] == "none"


def test_codex_disabled(write_config):
    write_config({"engines": {"codex": False}})
    config = load_config()
    assert route("review", "critical", config, state(), implementation_model="opus")["engine"] == "claude"
    assert route("implementation", "standard", config, state(claude="near_limit", claude_pct=20))["engine"] == "claude"


def test_claude_to_codex_fallback_disabled(write_config):
    write_config({"fallback": {"claude_to_codex": False}})
    assert route("implementation", "standard", load_config(), state(claude="limited", claude_pct=0))["engine"] == "none"


def test_scout_routing(config):
    assert route("scout", "standard", config, state())["model"] == "haiku"
    assert route("scout", "complex", config, state(claude="limited"))["engine"] == "codex"


def test_architecture_routing(config):
    assert route("architecture", "complex", config, state())["model"] == "opus"
    assert route("architecture", "complex", config, state(claude="limited"))["engine"] == "codex"
    constrained = route("architecture", "complex", config, state(claude="near_limit", codex="limited", claude_pct=20))
    assert (constrained["engine"], constrained["model"], constrained["requires_human_review"]) == ("claude", "opus", True)


@pytest.mark.parametrize("task_route, model, effort", [
    ("trivial", "gpt-6-luna", "low"),
    ("standard", "gpt-6.1-terra", "medium"),
    ("complex", "gpt-6.1-sol", "high"),
])
def test_codex_tier_equivalents_on_fallback(config, task_route, model, effort):
    result = route("implementation", task_route, config, state(claude="limited", claude_pct=0))
    assert (result["engine"], result["model"], result["effort"]) == ("codex", model, effort)


def test_codex_model_preferences_use_first_available(config, usage_files):
    write_models(usage_files, "gpt-6.1-terra", "gpt-5.6-terra")
    result = route("implementation", "standard", config, state(claude="limited", claude_pct=0))
    assert (result["engine"], result["model"]) == ("codex", "gpt-6.1-terra")


def test_codex_model_preferences_fall_back_for_plan(config, usage_files):
    write_models(usage_files, "gpt-5.6-terra")
    result = route("implementation", "standard", config, state(claude="limited", claude_pct=0))
    assert (result["engine"], result["model"]) == ("codex", "gpt-5.6-terra")


def test_unavailable_codex_model_routes_to_claude(config, usage_files):
    write_models(usage_files, "unrelated-model")
    result = route("review", "critical", config, state(), implementation_model="opus")
    assert (result["engine"], result["model"], result["requires_human_review"]) == ("claude", "sonnet", True)


def test_exclude_engine_moves_to_next(config):
    assert route("implementation", "standard", config, state(), exclude=("claude",))["engine"] == "codex"


def test_critical_never_implemented_off_claude(config):
    assert route("implementation", "critical", config, state(claude="limited", claude_pct=0))["engine"] == "none"
    near = route("implementation", "critical", config, state(claude="near_limit", claude_pct=20))
    assert (near["engine"], near["model"]) == ("claude", "opus")


def test_openrouter_enabled_without_key_is_ignored(config, monkeypatch):
    enable_openrouter(config, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    picks = {route("implementation", "standard", config, state(), task_key=f"t{i}")["engine"] for i in range(100)}
    assert "openrouter" not in picks


def test_three_way_split_uses_every_healthy_engine(config, monkeypatch):
    enable_openrouter(config, monkeypatch)
    picks = {route("implementation", "standard", config, state(), task_key=f"task-{i}")["engine"] for i in range(200)}
    assert picks == {"claude", "codex", "openrouter"}


def split(config, s, task_route="standard", n=400):
    picks = [route("implementation", task_route, config, s, task_key=f"task-{i}")["engine"] for i in range(n)]
    return picks.count("codex") / n


def test_adaptive_split_follows_headroom(config):
    assert 0.4 < split(config, state(claude_pct=75, codex_pct=75)) < 0.6
    assert split(config, state(claude_pct=95, codex_pct=35)) < 0.25
    assert split(config, state(claude_pct=35, codex_pct=95)) > 0.75


def test_adaptive_split_is_deterministic_and_scoped(config):
    s = state(claude_pct=60, codex_pct=60)
    first = route("implementation", "standard", config, s, task_key="same")
    assert all(route("implementation", "standard", config, s, task_key="same") == first for _ in range(5))
    assert "adaptive split" in first["reason"]
    assert split(config, s, "trivial") == 0 and split(config, s, "critical") == 0
    assert route("implementation", "standard", config, s)["engine"] == "claude", "no task key: no split"


def test_adaptive_split_can_be_turned_off(write_config):
    write_config({"balance": {"mode": "off"}})
    assert split(load_config(), state(claude_pct=30, codex_pct=100)) == 0


def test_supercritical_asks_interactively(config):
    assert route("review", "supercritical", config, state(), "t1")["effort"] == "ask"


def test_recorded_effort_wins(config):
    s = {**state(), "supercritical_efforts": {"t1": "max"}}
    assert route("review", "supercritical", config, s, "t1", permission_mode="auto")["effort"] == "max"


def test_auto_mode_inherits_parent(config):
    s = {**state(), "supercritical_efforts": {"p": "xhigh"}}
    r = route("review", "supercritical", config, s, "t2", parent_task_id="p", permission_mode="auto")
    assert r["effort"] == "xhigh"


def test_manual_mode_does_not_inherit(config):
    s = {**state(), "supercritical_efforts": {"p": "xhigh"}}
    r = route("review", "supercritical", config, s, "t2", parent_task_id="p", permission_mode="default")
    assert r["effort"] == "ask"


def test_headless_auto_defaults_medium(config):
    r = route("review", "supercritical", config, state(), "t2", permission_mode="auto", headless=True)
    assert (r["effort"], r["requires_human_review"]) == ("medium", False)


def test_headless_manual_needs_human(config):
    r = route("architecture", "supercritical", config, state(), "t2", headless=True)
    assert r["effort"] == "" and r["requires_human_review"] is True and r["engine"] != "none"
    assert "human must choose" in r["reason"]


def test_supercritical_architect_is_top_model(config):
    assert route("architecture", "supercritical", config, state(), "t1")["canonical_model"] == "fable"
