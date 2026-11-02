"""Offline tests for Codex status line rollout parsing."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "codex_status.py"


def run(home, project):
    return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps({"workspace": {"current_dir": str(project)}}),
                          text=True, capture_output=True, env={**os.environ, "HOME": str(home)})


def rollout(home, cwd, *items):
    directory = home / ".codex" / "sessions" / datetime.date.today().strftime("%Y/%m/%d")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "rollout-test.jsonl"
    lines = [{"type": "turn_context", "payload": {"cwd": str(cwd)}}, *items]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


def test_command_and_patch(tmp_path):
    project = tmp_path / "project"
    command = {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                                                       "arguments": json.dumps({"cmd": "pytest -q"})}}
    path = rollout(tmp_path, project / "subdir", command)
    assert "Codex ▸ running pytest -q" in run(tmp_path, project).stdout
    with path.open("a") as handle:
        handle.write(json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch",
                                                                       "input": "*** Update File: app.py\n"}}) + "\n")
    assert "Codex ▸ editing app.py" in run(tmp_path, project).stdout


def test_stale_other_and_missing(tmp_path):
    project = tmp_path / "project"
    assert run(tmp_path, project).stdout == ""
    path = rollout(tmp_path, tmp_path / "other", {"type": "event_msg", "payload": {"type": "task_started"}})
    assert run(tmp_path, project).stdout == ""
    path = rollout(tmp_path, project, {"type": "event_msg", "payload": {"type": "task_started"}})
    os.utime(path, (time.time() - 30, time.time() - 30))
    assert run(tmp_path, project).stdout == ""


def test_malformed_lines(tmp_path):
    path = rollout(tmp_path, tmp_path / "project")
    path.write_text("bad json\n{}\n")
    result = run(tmp_path, tmp_path / "project")
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""


def test_real_exec_rollout_and_idle(tmp_path):
    project = tmp_path / "project"
    # Trimmed rollout shape; command and path are anonymized.
    item = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
             "input": 'text(await tools.exec_command({cmd:"python3 -m pytest -q"}));'}}
    path = rollout(tmp_path, project, item)
    assert "running python3 -m pytest -q" in run(tmp_path, project).stdout
    patch = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
             "input": 'await tools.apply_patch(`*** Begin Patch\n*** Update File: app.py\n*** End Patch`);'}}
    with path.open("a") as handle:
        handle.write(json.dumps(patch) + "\n")
    assert "editing app.py" in run(tmp_path, project).stdout
    for kind in ("task_complete", "turn_aborted"):
        with path.open("a") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": {"type": kind}}) + "\n")
        assert run(tmp_path, project).stdout == ""


def test_recent_rollout_in_older_date_directory(tmp_path):
    project = tmp_path / "project"
    path = rollout(tmp_path, project, {"type": "event_msg", "payload": {"type": "task_started"}})
    old = tmp_path / ".codex/sessions/2020/01/01"
    old.mkdir(parents=True)
    path.rename(old / path.name)
    assert "thinking" in run(tmp_path, project).stdout
