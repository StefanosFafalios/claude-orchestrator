import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

import openrouter_budget as ob
import pytest
from engine_state import load_config, load_state, sync_usage
from usage_sources import observation, openrouter_models, openrouter_usage

MID_MONTH = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
DAY = 86400


def snap(observed_at=MID_MONTH, **kw):
    return {"usage_daily": 0, "usage_weekly": 0, "usage_monthly": 0, "limit": None,
            "limit_remaining": None, "limit_reset": None, "observed_at": observed_at, **kw}


def budget():
    return ob.caps(240)


def enable(config, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secret")
    config["engines"]["openrouter"] = True
    config["openrouter"]["budget"] = budget()
    return config


def test_caps_from_yearly():
    assert ob.caps(240) == {"yearly_usd": 240, "monthly_usd": 20.0, "weekly_usd": 9.23, "daily_usd": 1.97}


def test_pace_ahead_is_near_limit_not_limited():
    obs = observation(ob.windows(snap(usage_monthly=15), budget(), MID_MONTH), MID_MONTH, "t", MID_MONTH)
    assert 0 < obs["remaining_pct"] < 25


def test_spent_daily_cap_is_limited():
    assert ob.cap_spent(snap(usage_daily=2.0), budget(), MID_MONTH) is True
    assert ob.cap_spent(snap(usage_monthly=15), budget(), MID_MONTH) is False
    assert ob.cap_spent(snap(limit_remaining=0), budget(), MID_MONTH) is True


def test_stale_key_limit_ignored_after_its_reset():
    stale = MID_MONTH - 2 * DAY
    assert ob.cap_spent(snap(observed_at=stale, limit_remaining=0, limit_reset="daily"), budget(), MID_MONTH) is False
    assert ob.cap_spent(snap(observed_at=stale, limit_remaining=0, limit_reset="monthly"), budget(), MID_MONTH) is True
    assert ob.cap_spent(snap(observed_at=stale, limit_remaining=0), budget(), MID_MONTH) is True


def test_previous_day_spend_does_not_count_today():
    old = snap(observed_at=MID_MONTH - 2 * DAY, usage_daily=5)
    assert ob.windows(old, budget(), MID_MONTH)[0][0] == 0


def test_window_resets_are_utc_boundaries():
    _, week, month, _ = ob.windows(snap(), budget(), MID_MONTH)
    assert datetime.fromtimestamp(week[1], timezone.utc) == datetime(2026, 11, 16, tzinfo=timezone.utc)  # Monday
    assert datetime.fromtimestamp(month[1], timezone.utc) == datetime(2026, 12, 1, tzinfo=timezone.utc)


def test_zero_caps_are_skipped():
    assert ob.windows(snap(), {"daily_usd": 0, "weekly_usd": 0, "monthly_usd": 0}, MID_MONTH) == []


def test_over_soft_cap():
    assert ob.over_soft_cap(snap(usage_daily=2), budget()) == "daily"
    assert ob.over_soft_cap(snap(usage_weekly=10), budget()) == "weekly"
    assert ob.over_soft_cap(snap(), budget()) is None


def test_no_snapshot_means_no_observation(project, monkeypatch):
    assert openrouter_usage(enable(load_config(), monkeypatch)) is None


def test_disabled_openrouter_has_no_observation(project):
    ob.write_snapshot({"usage_daily": 1}, MID_MONTH)
    assert openrouter_usage(load_config(), MID_MONTH) is None


def test_snapshot_never_contains_key():
    ob.write_snapshot({"usage_daily": 1, "label": "sk-or-v1-abc", "key": "sk-or-v1-abc"}, MID_MONTH)
    text = ob.usage_file().read_text()
    assert "sk-or" not in text and json.loads(text)["observed_at"] == MID_MONTH


def test_sync_applies_openrouter_observation(project, monkeypatch):
    config = enable(load_config(), monkeypatch)
    ob.write_snapshot({"usage_daily": 5}, MID_MONTH)
    state = load_state()
    assert sync_usage(state, config, {"openrouter": openrouter_usage(config, MID_MONTH)})
    assert state["openrouter"]["status"] == "limited"


def test_models_cache():
    assert openrouter_models() is None
    ob.models_file().write_text(json.dumps({"models": ["a/b"]}))
    assert openrouter_models() == {"a/b"}


def test_refresh_and_verify_use_the_api(project, monkeypatch, fake_openrouter, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secret")
    fake_openrouter.models = [{"id": "x/y"}]
    fake_openrouter.key_data.update(usage_daily=3, limit=20, limit_reset="monthly", label="sk-or-v1-secret")
    assert ob.main(["refresh"]) == 0
    assert openrouter_models() == {"x/y"}
    assert ob.read_snapshot()["usage_daily"] == 3
    assert "sk-or" not in ob.usage_file().read_text()
    capsys.readouterr()
    assert ob.main(["verify"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["key_valid"] is True and "sk-or" not in out


def test_verify_fails_without_key(project, fake_openrouter):
    assert ob.main(["verify"]) == 1


def test_fake_server_rejects_missing_bearer(fake_openrouter):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(fake_openrouter.base + "/key")
    assert exc.value.code == 401


def limited_state():
    state = load_state()
    state["openrouter"].update(status="limited", remaining_pct=0, last_checked="2026-11-15T12:00:00+00:00")
    return state


def test_limited_openrouter_recovers_after_its_window_rolls_over(project, monkeypatch):
    config = enable(load_config(), monkeypatch)
    ob.write_snapshot({"usage_daily": 2.0, "usage_weekly": 2.0, "usage_monthly": 2.0}, MID_MONTH - DAY)  # yesterday's cap
    state = limited_state()
    assert sync_usage(state, config, {"openrouter": openrouter_usage(config, MID_MONTH)})
    assert state["openrouter"]["status"] == "healthy"


def test_limited_openrouter_stays_limited_while_a_cap_is_spent(project, monkeypatch):
    config = enable(load_config(), monkeypatch)
    ob.write_snapshot({"usage_weekly": 9.5}, MID_MONTH - DAY)  # the weekly cap still binds today
    state = limited_state()
    sync_usage(state, config, {"openrouter": openrouter_usage(config, MID_MONTH)})
    assert state["openrouter"]["status"] == "limited"
    ob.write_snapshot({"usage_daily": 0.5}, MID_MONTH - 3600)  # same day, before the limit was marked
    sync_usage(state, config, {"openrouter": openrouter_usage(config, MID_MONTH)})
    assert state["openrouter"]["status"] == "limited"
