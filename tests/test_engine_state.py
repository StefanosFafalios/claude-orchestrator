import json
import os

import pytest

import engine_state
from engine_state import ConfigError, load_config, load_state, main, save_state, state_path


def read_state(project):
    return json.loads((project / ".claude" / "engine-state.json").read_text())


def test_default_state_when_missing(project, capsys):
    assert main(["status"]) == 0
    state = json.loads(capsys.readouterr().out)
    for engine in ("claude", "codex"):
        assert state[engine] == {"status": "healthy", "remaining_pct": 100, "last_error": None, "last_checked": None}
    assert not state_path().exists(), "status must not write state"


def test_atomic_write_replaces_file_and_leaves_no_temp(project):
    save_state(load_state())
    files = os.listdir(project / ".claude")
    assert files == ["engine-state.json"]


def test_failed_write_keeps_previous_state(project, monkeypatch):
    main(["set", "--engine", "claude", "--remaining-pct", "50"])
    before = (project / ".claude" / "engine-state.json").read_text()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(engine_state.os, "replace", boom)
    with pytest.raises(OSError):
        main(["mark-limit", "--engine", "claude"])
    assert (project / ".claude" / "engine-state.json").read_text() == before
    assert os.listdir(project / ".claude") == ["engine-state.json"]


def test_mark_limit_persists_error_and_timestamp(project):
    main(["mark-limit", "--engine", "codex", "--message", "You've hit your usage limit. Resets 5pm"])
    codex = read_state(project)["codex"]
    assert codex["status"] == "limited"
    assert codex["remaining_pct"] == 0
    assert codex["last_error"].startswith("You've hit your usage limit")
    assert codex["last_checked"]
    assert read_state(project)["claude"]["status"] == "healthy"


def test_mark_limit_redacts_secrets(project):
    main(["mark-limit", "--engine", "claude", "--message", "limit hit; token=abc123 key sk-abcdefghijklmnopqrstuv"])
    error = read_state(project)["claude"]["last_error"]
    assert "abc123" not in error and "sk-abcdef" not in error
    assert "[REDACTED]" in error


def test_set_derives_near_limit_from_threshold(project):
    main(["set", "--engine", "claude", "--remaining-pct", "20"])
    assert read_state(project)["claude"]["status"] == "near_limit"
    main(["set", "--engine", "claude", "--remaining-pct", "80"])
    assert read_state(project)["claude"]["status"] == "healthy"
    main(["set", "--engine", "claude", "--status", "limited", "--remaining-pct", "0"])
    assert read_state(project)["claude"]["status"] == "limited"


def test_reset_restores_defaults(project):
    main(["mark-limit", "--engine", "claude"])
    main(["reset"])
    state = read_state(project)
    assert state["claude"]["status"] == "healthy"
    assert state["claude"]["last_error"] is None


def test_record_result(project):
    main(["mark-limit", "--engine", "codex"])
    main(["record-result", "--engine", "codex", "--task-id", "t1", "--status", "failed"])
    assert read_state(project)["codex"]["status"] == "limited"
    main(["record-result", "--engine", "codex", "--task-id", "t2", "--status", "success"])
    codex = read_state(project)["codex"]
    assert codex["status"] == "healthy"
    assert codex["last_result"]["task_id"] == "t2"
    assert codex["last_checked"]


def test_project_config_overrides_only_given_fields(write_config):
    write_config({"thresholds": {"near_limit_pct": 40}, "commands": {"test": "make test"}, "unknown": {"x": 1}})
    config = load_config()
    assert config["thresholds"]["near_limit_pct"] == 40
    assert config["thresholds"]["critical_limit_pct"] == 10
    assert config["commands"]["test"] == "make test"
    assert config["defaults"]["scout_model"] == "haiku"
    assert config["automatic"] is True


def test_invalid_config_is_a_clear_error(write_config, capsys):
    write_config("{not json")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_config()
    assert main(["status"]) == 2
    assert "invalid JSON" in capsys.readouterr().err


def test_corrupt_state_falls_back_to_defaults(project):
    (project / ".claude" / "engine-state.json").write_text("garbage")
    assert load_state()["claude"]["status"] == "healthy"
