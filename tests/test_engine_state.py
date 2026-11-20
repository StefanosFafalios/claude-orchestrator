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
    assert "defaults" not in config
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


def test_openrouter_disabled_without_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config = {"engines": {"openrouter": True}, "openrouter": {"key_env": "OPENROUTER_API_KEY"}}
    assert engine_state.engine_enabled(config, "openrouter") is False


def test_openrouter_enabled_with_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    config = {"engines": {"openrouter": True}, "openrouter": {"key_env": "OPENROUTER_API_KEY"}}
    assert engine_state.engine_enabled(config, "openrouter") is True
    assert engine_state.engine_enabled({"engines": {}}, "codex") is True


def test_state_has_three_engines(project):
    assert set(engine_state.ENGINES) <= set(load_state())


def test_set_effort_records_and_validates(project, capsys):
    assert main(["set-effort", "--task-id", "t1", "--effort", "high"]) == 0
    assert load_state()["supercritical_efforts"] == {"t1": "high"}
    assert main(["set-effort", "--task-id", "../x", "--effort", "high"]) == 2
    main(["reset"])
    assert load_state()["supercritical_efforts"] == {}


def test_enable_and_disable_engine_keep_other_keys(project, write_config):
    path = write_config({"commands": {"test": "make test"}})
    assert main(["enable", "openrouter"]) == 0
    raw = json.loads(path.read_text())
    assert raw == {"commands": {"test": "make test"}, "engines": {"openrouter": True}}
    assert load_config()["engines"]["openrouter"] is True
    assert main(["disable", "openrouter"]) == 0
    assert json.loads(path.read_text())["engines"] == {"openrouter": False}


def test_enable_creates_config_and_never_clobbers_invalid_json(project, write_config):
    assert main(["enable", "openrouter"]) == 0
    assert json.loads((project / ".claude" / "orchestrator.config.json").read_text()) == {"engines": {"openrouter": True}}
    path = write_config("{oops")
    assert main(["disable", "openrouter"]) == 2
    assert path.read_text() == "{oops"
