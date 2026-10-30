import json
import os
import time

from engine_state import load_config, load_state, main, sync_usage
from usage_sources import claude_usage, codex_usage

NOW = time.time()


def write_claude(usage_files, five, seven=0.0, reset_in=3600, age=0):
    path = usage_files / "statusline-input.json"
    path.write_text(json.dumps({"model": {"id": "x"}, "rate_limits": {
        "five_hour": {"used_percentage": five, "resets_at": NOW + reset_in},
        "seven_day": {"used_percentage": seven, "resets_at": NOW + 86400}}}))
    os.utime(path, (NOW - age, NOW - age))


def write_codex(usage_files, primary, secondary=10.0, reset_in=3600):
    day = usage_files / "codex" / "sessions" / "2026" / "10" / "29"
    day.mkdir(parents=True, exist_ok=True)
    event = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": primary, "window_minutes": 300, "resets_at": NOW + reset_in},
        "secondary": {"used_percent": secondary, "window_minutes": 10080, "resets_at": NOW + 86400}}}}
    (day / "rollout-a.jsonl").write_text('{"type":"session_meta"}\n' + json.dumps(event) + "\n")


def test_no_sources_means_no_observation():
    assert claude_usage() is None and codex_usage() is None


def test_readers_use_highest_active_window(usage_files):
    write_claude(usage_files, five=30, seven=80)
    write_codex(usage_files, primary=76, secondary=12)
    assert claude_usage()["remaining_pct"] == 20
    codex = codex_usage()
    assert codex["remaining_pct"] == 24 and codex["source"] == "codex-session-log"


def test_window_past_reset_counts_as_unused(usage_files):
    write_codex(usage_files, primary=100, secondary=5, reset_in=-60)
    obs = codex_usage()
    assert obs["remaining_pct"] == 95 and not obs["windows_reset"]


def test_status_syncs_observed_usage(project, usage_files, capsys):
    write_claude(usage_files, five=90)
    write_codex(usage_files, primary=40)
    assert main(["status"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert (state["claude"]["status"], state["claude"]["remaining_pct"]) == ("near_limit", 10)
    assert (state["codex"]["status"], state["codex"]["remaining_pct"]) == ("healthy", 60)
    assert state["codex"]["usage_source"] == "codex-session-log"
    assert load_state()["claude"]["status"] == "near_limit", "synced state is persisted"


def test_exhausted_window_marks_limited(project, usage_files):
    write_codex(usage_files, primary=100)
    main(["sync"])
    assert load_state()["codex"]["status"] == "limited"


def test_stale_observation_does_not_override_newer_limit(project, usage_files):
    write_claude(usage_files, five=20, age=600)
    main(["mark-limit", "--engine", "claude", "--message", "hit your session limit"])
    main(["sync"])
    assert load_state()["claude"]["status"] == "limited"


def test_limit_clears_once_all_windows_reset(project, usage_files):
    main(["mark-limit", "--engine", "codex"])
    state = load_state()
    obs = {"codex": {"remaining_pct": 100.0, "observed_at": NOW - 3600, "resets_at": None,
                     "windows_reset": True, "source": "codex-session-log"}}
    assert sync_usage(state, load_config(), obs)
    assert state["codex"]["status"] == "healthy" and state["codex"]["last_error"] is None


def test_usage_sync_can_be_disabled(project, usage_files, write_config):
    write_config({"usage_sync": False})
    write_codex(usage_files, primary=100)
    main(["sync"])
    assert load_state()["codex"]["status"] == "healthy"


def test_reset_time_comes_from_the_binding_window(usage_files):
    write_claude(usage_files, five=12, seven=98, reset_in=3600)
    obs = claude_usage()
    assert obs["remaining_pct"] == 2
    assert obs["resets_at"] > NOW + 80000, "weekly window binds, not the sooner 5-hour reset"
