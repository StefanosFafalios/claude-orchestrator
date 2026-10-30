import pytest

from engine_state import load_config
from route_engine import route


def state(claude="healthy", codex="healthy", claude_pct=100, codex_pct=100):
    def entry(status, pct):
        return {"status": status, "remaining_pct": pct, "last_error": None, "last_checked": None}
    return {"claude": entry(claude, claude_pct), "codex": entry(codex, codex_pct)}


@pytest.fixture
def config(project):
    return load_config()


@pytest.mark.parametrize("task_route, model, effort", [
    ("trivial", "haiku", "low"),
    ("standard", "sonnet", "medium"),
    ("complex", "sonnet", "high"),
    ("critical", "opus", "high"),
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
    both = state(claude="near_limit", codex="near_limit", claude_pct=10, codex_pct=20)
    assert route("review", "standard", config, both)["engine"] == "codex"


@pytest.mark.parametrize("claude_pct, codex_pct, expected", [(20, 10, "claude"), (5, 22, "codex")])
def test_both_near_limit(config, claude_pct, codex_pct, expected):
    s = state(claude="near_limit", codex="near_limit", claude_pct=claude_pct, codex_pct=codex_pct)
    for role in ("implementation", "review"):
        result = route(role, "standard", config, s)
        assert result["engine"] == expected
        assert result["requires_human_review"] is True
    assert route("implementation", "standard", config, s)["scope"] == "single_atomic_task"


def test_low_remaining_pct_counts_as_near_limit(config):
    assert route("implementation", "standard", config, state(claude_pct=10))["engine"] == "codex"


def test_both_limited_returns_none(config):
    result = route("implementation", "standard", config, state("limited", "limited", 0, 0))
    assert result["engine"] == "none"
    assert result["requires_human_review"] is True


def test_healthy_review_routing(config):
    assert route("review", "critical", config, state())["engine"] == "codex"
    assert route("review", "complex", config, state(), requires_codex_review=True)["engine"] == "codex"
    complex_review = route("review", "complex", config, state())
    assert (complex_review["engine"], complex_review["model"], complex_review["effort"]) == ("claude", "sonnet", "medium")
    trivial = route("review", "trivial", config, state())
    assert (trivial["engine"], trivial["model"], trivial["effort"]) == ("claude", "haiku", "low")


def test_codex_disabled(write_config):
    write_config({"engines": {"codex": False}})
    config = load_config()
    assert route("review", "critical", config, state())["engine"] == "claude"
    assert route("implementation", "standard", config, state(claude="near_limit", claude_pct=20))["engine"] == "claude"


def test_claude_to_codex_fallback_disabled(write_config):
    write_config({"fallback": {"claude_to_codex": False}})
    assert route("implementation", "standard", load_config(), state(claude="limited", claude_pct=0))["engine"] == "none"


def test_project_model_overrides(write_config):
    write_config({"defaults": {"implementation_model": "opus", "implementation_effort": "high"},
                  "codex": {"tiers": {"standard": {"model": "my-codex", "effort": "low"}}}})
    config = load_config()
    impl = route("implementation", "standard", config, state())
    assert (impl["model"], impl["effort"]) == ("opus", "high")
    codex = route("implementation", "standard", config, state(claude="limited"))
    assert (codex["model"], codex["effort"]) == ("my-codex", "low")


def test_scout_routing(config):
    assert route("scout", "standard", config, state())["model"] == "haiku"
    constrained = state(claude="near_limit", claude_pct=20)
    assert route("scout", "complex", config, constrained)["engine"] == "codex"
    assert route("scout", "trivial", config, constrained)["engine"] == "none"


def test_architecture_routing(config):
    assert route("architecture", "complex", config, state())["model"] == "opus"
    assert route("architecture", "complex", config, state(claude="limited"))["engine"] == "codex"
    downgraded = route("architecture", "complex", config, state(claude="near_limit", codex="limited", claude_pct=20))
    assert (downgraded["engine"], downgraded["model"], downgraded["effort"]) == ("claude", "sonnet", "high")


@pytest.mark.parametrize("task_route, model, effort", [
    ("trivial", "gpt-6-luna", "low"),
    ("standard", "gpt-6.1-terra", "medium"),
    ("complex", "gpt-6.1-sol", "high"),
])
def test_codex_tier_equivalents_on_fallback(config, task_route, model, effort):
    result = route("implementation", task_route, config, state(claude="limited", claude_pct=0))
    assert (result["engine"], result["model"], result["effort"]) == ("codex", model, effort)


def test_critical_stays_on_claude(config):
    assert route("implementation", "critical", config, state(claude="limited", claude_pct=0))["engine"] == "none"
    near = route("implementation", "critical", config, state(claude="near_limit", claude_pct=20))
    assert (near["engine"], near["model"]) == ("claude", "opus")
    assert "switched off for critical" in near["reason"]
    review = route("review", "critical", config, state())
    assert (review["engine"], review["model"]) == ("codex", "gpt-6-astra"), "codex may still review critical work"


def test_tier_switch_is_per_route(write_config):
    write_config({"codex": {"tiers": {"standard": {"implement": False}}}})
    config = load_config()
    assert route("implementation", "standard", config, state(claude="limited", claude_pct=0))["engine"] == "none"
    assert route("implementation", "complex", config, state(claude="limited", claude_pct=0))["engine"] == "codex"


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


def test_cross_engine_review(config):
    s = state()
    assert route("review", "standard", config, s, implementation_engine="claude")["engine"] == "codex"
    claude_review = route("review", "standard", config, s, implementation_engine="codex")
    assert (claude_review["engine"], claude_review["model"]) == ("claude", "sonnet")
    auto = [route("review", "standard", config, s, task_key=f"t{i}", implementation_engine="auto") for i in range(50)]
    impls = [route("implementation", "standard", config, s, task_key=f"t{i}")["engine"] for i in range(50)]
    assert all(r["engine"] != e for r, e in zip(auto, impls))
