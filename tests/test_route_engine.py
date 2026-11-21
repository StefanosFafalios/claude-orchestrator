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
    # complex row [sol 82, opus 85, sonnet 70, luna 55]: nothing >= 85 except the implementer itself,
    # so a near-equal (within SIMILAR_POINTS) different model reviews without a human
    r = route("review", "complex", config, state(), implementation_model="opus")
    assert r["canonical_model"] == "gpt-6.1-sol" and r["requires_human_review"] is False
    weaker = route("review", "complex", config, state(codex="limited"), implementation_model="opus")
    assert weaker["canonical_model"] == "sonnet" and weaker["requires_human_review"] is True
    assert "no reviewer at least as capable" in weaker["reason"]
    stronger = route("review", "complex", config, state(), implementation_model="gpt-6.1-sol")
    assert stronger["canonical_model"] == "opus" and stronger["requires_human_review"] is False


def test_review_excludes_every_model_that_wrote_code(project, capsys):
    """An escalated fix adds a second writer: the reviewer is neither, and at least as capable as both."""
    from route_engine import main
    assert main(["--role", "review", "--implementation-model", "gpt-6.1-terra", "--implementation-model", "sonnet"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert (r["canonical_model"], r["requires_human_review"]) == ("opus", False)


def test_trivial_tasks_get_no_review_or_scout(config):
    for role in ("review", "style_review", "scout"):
        r = route(role, "trivial", config, state(), implementation_model="haiku")
        assert (r["engine"], r["requires_human_review"]) == ("none", False), role


@pytest.mark.parametrize("only", ["claude", "codex"])
@pytest.mark.parametrize("task_route", ["standard", "complex", "critical", "supercritical"])
def test_shipped_review_rows_cover_every_implementer(write_config, monkeypatch, only, task_route):
    """Cheapest-first review rows hold a stronger or near-equal, different reviewer on a single engine.

    Fable and Astra are reserved for supercritical work, so the strongest model below it (Opus,
    or GPT-6.2-sol on Codex) is reviewed by a near-equal one; where the engine alone has none
    (Codex, or Claude at supercritical without OpenRouter), a human checks.
    """
    from route_engine import SIMILAR_POINTS
    # Accepted gaps (owner rule: top models only in supercritical rows): no same-engine reviewer
    # within reach, so a human checks. Any other gap is a template regression.
    gaps = {("codex", "gpt-6.2-sol", "standard"), ("codex", "gpt-6.2-sol", "complex"),
            ("claude", "opus", "supercritical")}
    monkeypatch.setenv("ORCHESTRATOR_DEFAULT_CONFIG", str(TEMPLATE))
    write_config({"engines": {"claude": only == "claude", "codex": only == "codex"}})
    config = load_config()
    for model in row(config, "implementation", task_route)["models"]:
        if not engine_name(config, model, only):
            continue
        r = route("review", task_route, config, state(), implementation_model=model)
        if (only, model, task_route) in gaps:
            assert r["requires_human_review"] is True and r["canonical_model"] != model, (model, r)
            continue
        assert r["engine"] == only and r["canonical_model"] != model, (model, r)
        assert r["requires_human_review"] is False, (model, r)
        assert score(config, r["canonical_model"]) >= score(config, model) - SIMILAR_POINTS, (model, r)


def test_shipped_top_models_stay_below_supercritical_review():
    """Owner rule: Opus work below supercritical gets a near-equal reviewer, never Fable or Astra."""
    routing = json.loads(TEMPLATE.read_text())["routing"]
    for role, rows in routing.items():
        for task_route, entry in rows.items():
            if task_route != "supercritical":
                assert not {"fable", "gpt-6.2-astra"} & set(entry["models"]), (role, task_route)


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
            if (role, task_route) == ("review", "supercritical"):
                assert entry["models"] == ["fable", "gpt-6.2-astra"]
            elif (role, task_route) == ("architecture", "supercritical"):  # Opus plans when Fable can't run
                assert entry["models"] == ["fable", "gpt-6.2-astra", "opus"]
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


def test_critical_waits_for_claude_when_no_other_engine_serves_opus(config):
    # The frozen map serves opus on Claude only; the shipped map also has it on OpenRouter (R30).
    assert route("implementation", "critical", config, state(claude="limited", claude_pct=0))["engine"] == "none"
    near = route("implementation", "critical", config, state(claude="near_limit", claude_pct=20))
    assert (near["engine"], near["model"]) == ("claude", "opus")


def shipped_config(write_config, monkeypatch, openrouter):
    monkeypatch.setenv("ORCHESTRATOR_DEFAULT_CONFIG", str(TEMPLATE))
    if openrouter:
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    write_config({"engines": {"openrouter": openrouter}})
    return load_config()


@pytest.mark.parametrize("claude, pct", [("near_limit", 20), ("limited", 0)])
def test_shipped_critical_implementation_moves_to_opus_on_openrouter(write_config, monkeypatch, claude, pct):
    """Same model, other engine: critical work keeps Opus while Claude cannot serve it (R30)."""
    result = route("implementation", "critical", shipped_config(write_config, monkeypatch, True), state(claude=claude, claude_pct=pct))
    assert (result["engine"], result["model"], result["canonical_model"]) == ("openrouter", "anthropic/claude-opus-5.5", "opus")
    assert result["requires_human_review"] is False


def test_shipped_critical_implementation_stops_without_openrouter(write_config, monkeypatch):
    result = route("implementation", "critical", shipped_config(write_config, monkeypatch, False), state(claude="limited", claude_pct=0))
    assert (result["engine"], result["requires_human_review"]) == ("none", True)


def test_openrouter_enabled_without_key_is_ignored(config, monkeypatch):
    enable_openrouter(config, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    picks = {route("implementation", "standard", config, state(), task_key=f"t{i}")["engine"] for i in range(100)}
    assert "openrouter" not in picks


def test_three_way_split_uses_every_healthy_engine(config, monkeypatch):
    enable_openrouter(config, monkeypatch)
    picks = {route("implementation", "standard", config, state(), task_key=f"task-{i}")["engine"] for i in range(200)}
    assert picks == {"claude", "codex", "openrouter"}


@pytest.mark.parametrize("home, model", [("codex", "gpt-6.1-terra"), ("claude", "sonnet")])
def test_split_prefers_healthy_home_for_same_model(config, monkeypatch, home, model):
    enable_openrouter(config, monkeypatch)
    config["models"][model]["engines"]["openrouter"] = "vendor/shared"
    models = config["routing"]["implementation"]["standard"]["models"]
    models.remove(model)
    models.insert(0, model)
    models.remove("or-model")
    picks = {route("implementation", "standard", config, state(), task_key=f"task-{i}")["engine"] for i in range(200)}
    assert home in picks and "openrouter" not in picks


def test_split_excludes_openrouter_models_served_later_by_healthy_engine(config, monkeypatch):
    enable_openrouter(config, monkeypatch)
    config["models"]["opus"]["engines"].pop("openrouter", None)
    config["models"]["sonnet"]["engines"]["openrouter"] = "vendor/sonnet"
    config["routing"]["implementation"]["standard"]["models"] = ["opus", "sonnet"]
    picks = {route("implementation", "standard", config, state(), task_key=f"task-{i}")["engine"] for i in range(200)}
    assert picks == {"claude"}


def test_split_openrouter_uses_first_model_not_served_by_healthy_engine(config, monkeypatch):
    enable_openrouter(config, monkeypatch)
    config["models"]["sonnet"]["engines"]["openrouter"] = "vendor/sonnet"
    entry = config["routing"]["implementation"]["standard"]
    entry["models"] = ["sonnet", "or-model"]
    entry["model_effort"] = {"or-model": "low"}
    results = [route("implementation", "standard", config, state(), task_key=f"task-{i}") for i in range(200)]
    openrouter = [r for r in results if r["engine"] == "openrouter"]
    assert openrouter
    assert all((r["canonical_model"], r["model"], r["effort"]) ==
               ("or-model", "vendor/or-model", "low") for r in openrouter)


@pytest.mark.parametrize("codex_status", ["limited", "near_limit"])
def test_shared_model_uses_openrouter_when_codex_constrained(config, monkeypatch, codex_status):
    enable_openrouter(config, monkeypatch)
    config["models"]["gpt-6.1-terra"]["engines"]["openrouter"] = "vendor/shared"
    config["routing"]["implementation"]["standard"]["models"] = ["gpt-6.1-terra"]
    result = route("implementation", "standard", config, state(codex=codex_status, codex_pct=0), task_key="task")
    assert (result["engine"], result["canonical_model"]) == ("openrouter", "gpt-6.1-terra")


def test_shared_model_uses_openrouter_when_codex_excluded(config, monkeypatch):
    enable_openrouter(config, monkeypatch)
    config["models"]["gpt-6.1-terra"]["engines"]["openrouter"] = "vendor/shared"
    config["routing"]["implementation"]["standard"]["models"] = ["gpt-6.1-terra"]
    result = route("implementation", "standard", config, state(), task_key="task", exclude=("codex",))
    assert (result["engine"], result["canonical_model"]) == ("openrouter", "gpt-6.1-terra")


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


@pytest.mark.parametrize("role", ["review", "architecture"])
@pytest.mark.parametrize("headless", [False, True])
def test_default_supercritical_uses_medium_without_human_review(config, role, headless):
    result = route(role, "supercritical", config, state(), "t1", headless=headless)
    assert (result["effort"], result["requires_human_review"]) == ("medium", False)


def test_recorded_effort_wins_for_default_row(config):
    s = {**state(), "supercritical_efforts": {"t1": "high"}}
    assert route("review", "supercritical", config, s, "t1")["effort"] == "high"
    # The override is for the top-model rows only; cheap helper roles keep their row effort.
    assert route("scout", "supercritical", config, s, "t1")["effort"] == "low"


def test_default_row_inherits_parent_effort_in_any_permission_mode(config):
    s = {**state(), "supercritical_efforts": {"p": "xhigh"}}
    r = route("architecture", "supercritical", config, s, "t2", parent_task_id="p", permission_mode="default")
    assert r["effort"] == "xhigh"


def test_opt_in_ask_row_keeps_ask_flow(config):
    config["routing"]["review"]["supercritical"]["effort"] = "ask"
    config["routing"]["architecture"]["supercritical"]["effort"] = "ask"
    assert route("review", "supercritical", config, state(), "t1")["effort"] == "ask"
    s = {**state(), "supercritical_efforts": {"p": "xhigh"}}
    assert route("review", "supercritical", config, s, "t2", parent_task_id="p", permission_mode="auto")["effort"] == "xhigh"
    r = route("architecture", "supercritical", config, state(), "t2", headless=True)
    assert r["effort"] == "" and r["requires_human_review"] is True and r["engine"] != "none"
    assert "human must choose" in r["reason"]


def test_supercritical_architect_is_top_model(config):
    assert route("architecture", "supercritical", config, state(), "t1")["canonical_model"] == "fable"


def test_unavailable_engine_routes_like_a_disabled_one(config):
    st = state()
    st["codex"]["status"] = "unavailable"
    for role in ("implementation", "review", "scout"):
        assert route(role, "complex", config, st, implementation_model="sonnet")["engine"] != "codex", role
    assert "codex is unavailable" in route("review", "complex", config, st, implementation_model="sonnet")["reason"]
