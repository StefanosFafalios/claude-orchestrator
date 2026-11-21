import json
import os
import threading
import time

import pytest

import engine_state
from engine_state import ConfigError, load_config, load_state, main, redact, save_state, state_lock, state_path


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
    assert sorted(os.listdir(project / ".claude")) == ["engine-state.json", "orchestrator"]


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
    for bad in ("{oops", '{"engines": "yes"}', "[]", '{"routing": {"review": {"complex": {"models": ["nope"]}}}}'):
        path = write_config(bad)
        assert main(["disable", "openrouter"]) == 2
        assert path.read_text() == bad


def test_enable_openrouter_reports_key_presence_only(project, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    assert main(["enable", "openrouter"]) == 0
    out = capsys.readouterr().out
    assert "OPENROUTER_API_KEY is set" in out and "sk-or-secret" not in out
    monkeypatch.delenv("OPENROUTER_API_KEY")
    main(["enable", "openrouter"])
    assert "NOT set" in capsys.readouterr().out


def test_enable_keeps_the_config_file_mode(project, write_config):
    path = write_config({})
    path.chmod(0o640)
    assert main(["enable", "openrouter"]) == 0
    assert path.stat().st_mode & 0o777 == 0o640


def test_unavailable_lasts_until_success_or_reset(project, write_config):
    write_config({})
    main(["mark-unavailable", "--engine", "codex", "--message", "codex: model not supported"])
    state = read_state(project)["codex"]
    assert state["status"] == "unavailable" and state["last_error"] == "codex: model not supported"
    main(["record-result", "--engine", "codex", "--task-id", "t1", "--status", "failed"])
    assert read_state(project)["codex"]["status"] == "unavailable"
    main(["record-result", "--engine", "codex", "--task-id", "t2", "--status", "success"])
    assert read_state(project)["codex"]["status"] == "healthy"
    main(["mark-unavailable", "--engine", "codex"])
    main(["reset"])
    assert read_state(project)["codex"]["status"] == "healthy"


def test_observed_quota_never_clears_unavailable(project, write_config):
    write_config({})
    state = load_state()
    state["codex"].update(status="unavailable", last_checked=None)
    observations = {"codex": {"remaining_pct": 80.0, "observed_at": 2e9, "resets_at": None, "windows_reset": True, "source": "x"}}
    assert engine_state.sync_usage(state, load_config(), observations) is False
    assert state["codex"]["status"] == "unavailable"


def fake_codex_login(tmp_path, monkeypatch, text, code):
    fake = tmp_path / "codex"
    fake.write_text(f"#!/bin/sh\necho '{text}'\nexit {code}\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(fake))


def test_status_check_login_marks_and_clears(project, write_config, tmp_path, monkeypatch, capsys):
    write_config({})
    fake_codex_login(tmp_path, monkeypatch, "Not logged in", 1)
    assert main(["status"]) == 0 and read_state_or_none(project) is None, "plain status never calls codex"
    assert main(["status", "--check-login"]) == 0
    assert read_state(project)["codex"]["status"] == "unavailable"
    fake_codex_login(tmp_path, monkeypatch, "Logged in using ChatGPT", 0)
    main(["status", "--check-login"])
    assert read_state(project)["codex"]["status"] == "healthy"


def read_state_or_none(project):
    path = project / ".claude" / "engine-state.json"
    return json.loads(path.read_text()) if path.exists() else None


@pytest.mark.parametrize("message, cleared", [
    ("codex unavailable (logged_out) during t1", True),
    ("logged_out: Error: Not logged in", True),
    ("codex unavailable (model_unsupported) during t1", False),
])
def test_login_check_lifts_only_logout_marks(project, write_config, tmp_path, monkeypatch, message, cleared):
    write_config({})
    main(["mark-unavailable", "--engine", "codex", "--message", message])
    fake_codex_login(tmp_path, monkeypatch, "Logged in using ChatGPT", 0)
    main(["status", "--check-login"])
    assert (read_state(project)["codex"]["status"] == "healthy") is cleared


def test_logout_check_keeps_a_limited_mark(project, write_config, tmp_path, monkeypatch):
    write_config({})
    main(["mark-limit", "--engine", "codex"])
    fake_codex_login(tmp_path, monkeypatch, "Not logged in", 1)
    main(["status", "--check-login"])
    assert read_state(project)["codex"]["status"] == "limited"


@pytest.mark.parametrize("installed, latest, warned", [
    ("codex-cli 0.168.0", "0.171.0", True),
    ("codex-cli 0.171.0", "0.171.0", False),
    ("codex-cli 0.172.1", "0.171.0", False),
    ("codex-cli 0.168.0", None, False),  # registry unreachable: no warning, no failure
])
def test_check_login_warns_about_an_old_codex_cli(project, write_config, tmp_path, monkeypatch, capsys,
                                                    installed, latest, warned):
    write_config({})
    fake_codex_login(tmp_path, monkeypatch, installed, 0)
    monkeypatch.setattr(engine_state, "latest_codex_version", lambda: latest)
    assert main(["status", "--check-login"]) == 0
    assert ("is older than the latest" in capsys.readouterr().err) is warned


def test_plain_status_never_checks_the_version(project, write_config, monkeypatch):
    write_config({})
    monkeypatch.setattr(engine_state, "latest_codex_version", lambda: pytest.fail("network call in plain status"))
    assert main(["status"]) == 0


def test_values_only_redact_keeps_code_shape():
    assert redact("if token != expected:", values_only=True) == "if token != expected:"
    for text in ('api_key = "sk-or-v1-0123456789abcdefsecret"', 'password = "hunter22secret"',
                 "Authorization: Bearer abcdefghijklmnop1234", '"api_key": "hunter22secret"', "PASSWORD=hunter22secret"):
        out = redact(text, values_only=True)
        assert "[REDACTED]" in out and "hunter22" not in out and "0123456789" not in out and "abcdefghijklmnop" not in out
    assert redact('password = "hunter22secret"', values_only=True).startswith("password = ")
    for leak in ("password=P@ssw0rd123", "api_key=c2VjcmV0c2VjcmV0==", "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIx.abc",
                 "password: hunter2!", "password: hunter2", "DB_PASSWORD=hunter22secret",
                 "AWS_SECRET_ACCESS_KEY=hunter22secret", 'client_secret: "correct horse battery"'):
        assert "[REDACTED]" in redact(leak, values_only=True), leak
    assert redact("AWS_SECRET_ACCESS_KEY=hunter22secret", values_only=True) == "AWS_SECRET_ACCESS_KEY=[REDACTED]"
    for code in ("token = make_token()", "password = request.form", "token = self.token",
                 "secret_key = settings.SECRET_KEY", "token: Optional[str] = None", "if token == expected:"):
        assert redact(code, values_only=True) == code
    assert "abc" not in redact("token: abc")


def test_state_lock_serialises_concurrent_writers(project):
    thread = threading.Thread(target=main, args=(["mark-limit", "--engine", "codex"],))
    with state_lock(project):
        thread.start()
        time.sleep(0.2)
        assert thread.is_alive()
    thread.join(5)
    assert not thread.is_alive()
    assert read_state(project)["codex"]["status"] == "limited"


def test_state_lock_never_blocks_the_hook(project, monkeypatch):
    with state_lock(project):  # a stuck holder: the second taker gives up and runs unlocked
        start = time.monotonic()
        with state_lock(project, wait=0.1):
            pass
        assert time.monotonic() - start < 1
    monkeypatch.setattr(engine_state.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(PermissionError("read-only")))
    with state_lock(project):  # an unwritable project runs unlocked instead of failing
        pass
