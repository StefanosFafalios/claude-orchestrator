"""OpenRouter runner: exit-code contract and pause-and-ask supervision, using a fake codex."""

import json
import os
import signal
import subprocess
import sys
import time

import openrouter_budget as ob
import pytest
from conftest import ROOT

RUNNER = ROOT / "scripts" / "openrouter_run.py"
KEY = "sk-or-v1-0123456789abcdefsecret"

FAKE_CODEX = """#!/bin/sh
echo "$@" > "$FAKE_DIR/args"
echo x >> "$FAKE_DIR/calls"
if [ -n "${FAKE_ERR_ONCE:-}" ] && [ "$(wc -l < "$FAKE_DIR/calls")" -eq 1 ]; then
  echo "$FAKE_ERR_ONCE" >&2
  exit 1
fi
sleep "${FAKE_SLEEP:-0}"
printf '%s' "${FAKE_OUT:-}"
printf '%s' "${FAKE_ERR:-}" >&2
exit "${FAKE_CODE:-0}"
"""


@pytest.fixture
def env(project, write_config, fake_openrouter, tmp_path):
    write_config({"openrouter": {"budget": ob.caps(240)}})
    fake = tmp_path / "fake-codex"
    fake.write_text(FAKE_CODEX)
    fake.chmod(0o755)
    (tmp_path / "task.md").write_text("do the thing")
    return dict(os.environ, CODEX_BIN=str(fake), FAKE_DIR=str(tmp_path), OPENROUTER_MODEL="x/model",
                OPENROUTER_API_KEY=KEY, OPENROUTER_POLL_SECONDS="0.2")


def start(tmp_path, env, *args, **fake):
    argv = list(args) or [str(tmp_path / "task.md")]
    return subprocess.Popen([sys.executable, str(RUNNER), *argv], env=dict(env, **fake),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def run(tmp_path, env, *args, **fake):
    proc = start(tmp_path, env, *args, **fake)
    out, err = proc.communicate(timeout=10)
    return proc.returncode, out, err


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.05)


def pid_files(project):
    return list((project / ".claude" / "orchestrator" / "openrouter-runs").glob("*/pid"))


def state(pid):
    try:
        return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return None


def pause(project, tmp_path, env, fake_openrouter):
    proc = start(tmp_path, env, FAKE_SLEEP="5", FAKE_OUT="finished")
    wait_for(lambda: pid_files(project))
    fake_openrouter.key_data["usage_daily"] = 5
    out, _ = proc.communicate(timeout=4)
    assert proc.returncode == 43
    run_id = out.split("OPENROUTER_PAUSED ")[1].strip()
    return run_id, int(pid_files(project)[0].read_text())


def test_success_passes_output(tmp_path, env):
    code, out, _ = run(tmp_path, env, FAKE_OUT="all done")
    assert (code, out) == (0, "all done")
    assert ob.read_snapshot() is not None


def test_missing_key_is_ordinary_error(tmp_path, env):
    del env["OPENROUTER_API_KEY"]
    code, _, err = run(tmp_path, env)
    assert code == 1 and "OPENROUTER_API_KEY is not set" in err


def test_spent_cap_exits_42_before_running(tmp_path, env, fake_openrouter):
    fake_openrouter.key_data["usage_daily"] = 5
    code, out, _ = run(tmp_path, env)
    assert code == 42 and "OPENROUTER_USAGE_LIMIT" in out
    assert not (tmp_path / "args").exists()


def test_402_key_limit_exits_42(tmp_path, env):
    code, out, _ = run(tmp_path, env, FAKE_ERR="402 Payment Required: openrouter_key_limit", FAKE_CODE="1")
    assert code == 42 and out.strip() == "OPENROUTER_USAGE_LIMIT"


def test_auth_error_is_exit_1(tmp_path, env):
    code, out, err = run(tmp_path, env, FAKE_ERR="401 Unauthorized", FAKE_CODE="1")
    assert code == 1 and "OPENROUTER_USAGE_LIMIT" not in out and "401 Unauthorized" in err


def test_in_flight_budget_reruns_once(tmp_path, env):
    code, out, _ = run(tmp_path, env, FAKE_ERR_ONCE="openrouter_in_flight_budget", FAKE_OUT="ok")
    assert (code, out) == (0, "ok")
    assert len((tmp_path / "calls").read_text().split()) == 2


def test_over_daily_cap_pauses_with_43(project, tmp_path, env, fake_openrouter):
    run_id, pid = pause(project, tmp_path, env, fake_openrouter)
    paused = project / ".claude" / "orchestrator" / "openrouter-runs" / run_id / "paused.json"
    assert paused.is_file() and state(pid) == "T"
    run(tmp_path, env, "--stop", run_id)


def test_resume_does_not_repause_same_window(project, tmp_path, env, fake_openrouter):
    run_id, _ = pause(project, tmp_path, env, fake_openrouter)
    code, out, _ = run(tmp_path, env, "--resume", run_id)
    assert (code, out) == (0, "finished")


def test_stop_terminates_group(project, tmp_path, env, fake_openrouter):
    run_id, pid = pause(project, tmp_path, env, fake_openrouter)
    code, _, err = run(tmp_path, env, "--stop", run_id)
    assert code == 1 and "stopped by user" in err
    wait_for(lambda: state(pid) in (None, "Z"))


def test_bad_run_id_is_usage_error(tmp_path, env):
    assert run(tmp_path, env, "--resume", "../../etc")[0] == 2


def test_key_endpoint_down_does_not_change_exit(tmp_path, env, fake_openrouter):
    fake_openrouter.server.shutdown()
    fake_openrouter.server.server_close()
    assert run(tmp_path, env, FAKE_OUT="ok")[0] == 0


def test_key_never_in_args(tmp_path, env):
    run(tmp_path, env)
    args = (tmp_path / "args").read_text()
    assert KEY not in args
    assert 'model_providers.openrouter.env_key="OPENROUTER_API_KEY"' in args and "--model x/model" in args
    # Agent steps resend the whole context: it is compacted early and tool outputs are capped.
    assert "model_auto_compact_token_limit=64000" in args and "tool_output_token_limit=6000" in args


def test_stale_cached_snapshot_does_not_pause(tmp_path, env, fake_openrouter):
    ob.write_snapshot({"usage_daily": 5}, time.time() - 2 * 86400)
    fake_openrouter.server.shutdown()
    fake_openrouter.server.server_close()
    assert run(tmp_path, env, FAKE_SLEEP="1", FAKE_OUT="ok")[:2] == (0, "ok")


def test_runner_termination_stops_codex(project, tmp_path, env):
    proc = start(tmp_path, env, FAKE_SLEEP="5")
    wait_for(lambda: pid_files(project))
    pid = int(pid_files(project)[0].read_text())
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=5)
    assert proc.returncode == 1
    wait_for(lambda: state(pid) in (None, "Z"))


def test_repeated_in_flight_budget_exits_1(tmp_path, env):
    code, _, _ = run(tmp_path, env, FAKE_ERR="openrouter_in_flight_budget", FAKE_CODE="2")
    assert code == 1
    assert len((tmp_path / "calls").read_text().split()) == 2


@pytest.mark.parametrize("value, expected", [("/tmp/last.md", True), ("", False)])
def test_last_message_flag_is_passed_only_when_set(tmp_path, env, value, expected):
    code, _, _ = run(tmp_path, env, OPENROUTER_LAST_MESSAGE=value)
    words = (tmp_path / "args").read_text().split()
    assert code == 0 and ("-o" in words) is expected
    if expected:
        assert words[words.index("-o") + 1] == value and words.index("exec") < words.index("-o")


def test_run_over_its_allowance_pauses_and_resume_grants_one_more(project, tmp_path, env, fake_openrouter):
    # No daily/weekly caps: the per-run allowance still stops a runaway agent loop.
    (project / ".claude" / "orchestrator.config.json").write_text(json.dumps({"openrouter": {"budget": {"per_run_usd": 1}}}))
    fake_openrouter.key_data["usage"] = 10.0
    proc = start(tmp_path, env, FAKE_SLEEP="5", FAKE_OUT="finished")
    wait_for(lambda: pid_files(project))
    fake_openrouter.key_data["usage"] = 11.2
    out, _ = proc.communicate(timeout=4)
    assert proc.returncode == 43
    run_id = out.split("OPENROUTER_PAUSED ")[1].strip()
    paused = json.loads((project / ".claude" / "orchestrator" / "openrouter-runs" / run_id / "paused.json").read_text())
    assert (paused["window"], paused["base_usage"], paused["cap"]) == ("run", 10.0, 1)
    assert abs(paused["spend"] - 1.2) < 1e-9
    resumed = start(tmp_path, env, "--resume", run_id)
    time.sleep(0.6)
    fake_openrouter.key_data["usage"] = 12.1  # past the second allowance: pauses again
    out, _ = resumed.communicate(timeout=4)
    assert resumed.returncode == 43
    run(tmp_path, env, "--stop", run_id)


def test_zero_allowance_never_pauses(project, tmp_path, env, fake_openrouter):
    (project / ".claude" / "orchestrator.config.json").write_text(json.dumps({"openrouter": {"budget": {"per_run_usd": 0}}}))
    fake_openrouter.key_data["usage"] = 0
    proc = start(tmp_path, env, FAKE_SLEEP="1", FAKE_OUT="finished")
    wait_for(lambda: pid_files(project))
    fake_openrouter.key_data["usage"] = 50
    out, _ = proc.communicate(timeout=4)
    assert (proc.returncode, out) == (0, "finished")


FAKE_CLAUDE = """#!/bin/sh
echo "$@" > "$FAKE_DIR/claude-args"
printf '%s|%s|%s|%s' "$ANTHROPIC_API_KEY" "$ANTHROPIC_BASE_URL" "${ANTHROPIC_AUTH_TOKEN-unset}" "${OPENROUTER_API_KEY-unset}" > "$FAKE_DIR/claude-env"
printf 'final answer %s' "${FAKE_ECHO:-}"
"""


@pytest.fixture
def claude_env(env, tmp_path):
    fake = tmp_path / "fake-claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    return dict(env, CLAUDE_BIN=str(fake), OPENROUTER_MODEL="anthropic/claude-haiku-5.5", ANTHROPIC_AUTH_TOKEN="sub")


def test_anthropic_models_run_through_bare_claude_code(tmp_path, claude_env):
    # Claude Code marks prompt-cache breakpoints that Codex's requests lack; --bare never bills the subscription.
    code, out, _ = run(tmp_path, claude_env, OPENROUTER_EFFORT="medium", OPENROUTER_LAST_MESSAGE=str(tmp_path / "last.md"))
    assert (code, out) == (0, "final answer ")
    assert not (tmp_path / "args").exists(), "codex must not run"
    args = (tmp_path / "claude-args").read_text()
    assert "--no-session-persistence --model anthropic/claude-haiku-5.5" in args
    assert "-p --bare --restricted --strict-mcp-config" in args
    assert "--tools=Read,Grep,Glob --effort" in args and "Bash" not in args and "acceptEdits" not in args
    assert "--effort=medium -- do the thing" in args and KEY not in args
    key, base, token, or_key = (tmp_path / "claude-env").read_text().split("|")
    assert (key, base, token, or_key) == (KEY, ob.api_base()[:-3] if ob.api_base().endswith("/v1") else ob.api_base(), "unset", "unset")
    assert (tmp_path / "last.md").read_text() == "final answer "


def test_claude_code_write_runs_bash_only_in_its_sandbox(tmp_path, claude_env, monkeypatch):
    import openrouter_run
    run(tmp_path, claude_env, OPENROUTER_SANDBOX="workspace-write")
    args = (tmp_path / "claude-args").read_text()
    if openrouter_run.sandbox_available():
        assert "--tools=Read,Grep,Glob,Edit,Write,Bash --allowedTools=Bash --permission-mode=acceptEdits" in args
        assert '"allowUnsandboxedCommands": false, "failIfUnavailable": true' in args
    monkeypatch.setattr(openrouter_run, "sandbox_available", lambda: False)
    cmd = openrouter_run.claude_command("anthropic/x", "", "workspace-write", "p")
    assert "--tools=Read,Grep,Glob,Edit,Write" in cmd and not any("Bash" in a for a in cmd)


def test_claude_code_output_is_redacted(tmp_path, claude_env):
    code, out, _ = run(tmp_path, claude_env, FAKE_ECHO=KEY, OPENROUTER_LAST_MESSAGE=str(tmp_path / "last.md"))
    assert code == 0 and KEY not in out and KEY not in (tmp_path / "last.md").read_text()
