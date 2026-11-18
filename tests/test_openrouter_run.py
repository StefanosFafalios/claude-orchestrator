"""OpenRouter runner: exit-code contract and pause-and-ask supervision, using a fake codex."""

import os
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
