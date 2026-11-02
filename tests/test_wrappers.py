"""Wrapper exit-code contract, using fake CLIs (never the real claude/codex)."""

import os
import subprocess

import pytest
from conftest import ROOT

SCRIPTS = ROOT / "scripts"


def fake_cli(tmp_path, stdout="", stderr="", code=0):
    path = tmp_path / "fake-cli"
    (tmp_path / "out").write_text(stdout)
    (tmp_path / "err").write_text(stderr)
    path.write_text(f'#!/bin/sh\necho "$@" > "{tmp_path}/args"\ncat "{tmp_path}/out"\ncat "{tmp_path}/err" >&2\nexit {code}\n')
    path.chmod(0o755)
    return path


def run(wrapper, tmp_path, env_var, **fake):
    task = tmp_path / "task.md"
    task.write_text("do the thing")
    env = dict(os.environ, **{env_var: str(fake_cli(tmp_path, **fake))})
    return subprocess.run(["bash", str(SCRIPTS / wrapper), str(task)], capture_output=True, text=True, env=env)


WRAPPERS = [("claude_with_fallback.sh", "CLAUDE_BIN", "CLAUDE_USAGE_LIMIT"),
            ("codex_with_fallback.sh", "CODEX_BIN", "CODEX_USAGE_LIMIT")]


@pytest.mark.parametrize("wrapper, env_var, marker", WRAPPERS)
def test_success(tmp_path, wrapper, env_var, marker):
    r = run(wrapper, tmp_path, env_var, stdout="all done\n")
    assert r.returncode == 0 and r.stdout == "all done\n"


@pytest.mark.parametrize("wrapper, env_var, marker", WRAPPERS)
@pytest.mark.parametrize("message", ["You've hit your usage limit · resets 5pm", "Usage limit reached. Upgrade to Pro"])
def test_usage_limit_exits_42(tmp_path, wrapper, env_var, marker, message):
    r = run(wrapper, tmp_path, env_var, stderr=message, code=1)
    assert r.returncode == 42
    assert r.stdout.strip() == marker


def test_claude_specific_patterns(tmp_path):
    for message in ("You've hit your session limit", "You've hit your weekly limit", "Your limit has been reached"):
        assert run("claude_with_fallback.sh", tmp_path, "CLAUDE_BIN", stdout=message, code=1).returncode == 42


@pytest.mark.parametrize("wrapper, env_var, marker", WRAPPERS)
@pytest.mark.parametrize("message", [
    "Error: 401 Unauthorized - session resets required",
    "getaddrinfo ENOTFOUND api.example.com",
    "Permission denied: cannot write file",
    "TypeError: undefined is not a function",
])
def test_ordinary_errors_exit_1(tmp_path, wrapper, env_var, marker, message):
    r = run(wrapper, tmp_path, env_var, stderr=message, code=1)
    assert r.returncode == 1
    assert marker not in r.stdout
    assert message in r.stderr


@pytest.mark.parametrize("wrapper, env_var, marker", WRAPPERS)
def test_long_success_discussing_limits_is_success(tmp_path, wrapper, env_var, marker):
    answer = "Implemented the usage limit handler; it resets counters daily. " * 20
    assert run(wrapper, tmp_path, env_var, stdout=answer).returncode == 0


def test_codex_sandbox_and_model_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SANDBOX", "workspace-write")
    monkeypatch.setenv("CODEX_MODEL", "m1")
    run("codex_with_fallback.sh", tmp_path, "CODEX_BIN", stdout="ok")
    args = (tmp_path / "args").read_text()
    assert "exec --sandbox workspace-write --model m1" in args
    assert "do the thing" in args


@pytest.mark.parametrize("wrapper", ["claude_with_fallback.sh", "codex_with_fallback.sh"])
def test_bad_usage(wrapper):
    assert subprocess.run(["bash", str(SCRIPTS / wrapper)], capture_output=True).returncode == 2


def test_claude_permission_mode(tmp_path, monkeypatch):
    run("claude_with_fallback.sh", tmp_path, "CLAUDE_BIN", stdout="ok")
    assert "--permission-mode acceptEdits" in (tmp_path / "args").read_text()
    monkeypatch.setenv("CLAUDE_PERMISSION_MODE", "plan")
    run("claude_with_fallback.sh", tmp_path, "CLAUDE_BIN", stdout="ok")
    assert "--permission-mode plan" in (tmp_path / "args").read_text()
