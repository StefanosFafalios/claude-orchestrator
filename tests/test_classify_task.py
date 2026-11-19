import json
import subprocess
import sys

import pytest
from conftest import ROOT

from classify_task import classify


@pytest.mark.parametrize("task, route", [
    ("fix typo in README", "trivial"),
    ("What does this function do?", "trivial"),
    ("rename the helper to parse_row", "trivial"),
    ("add unit tests for parser", "standard"),
    ("Implement retry handling for webhook failures and update tests", "standard"),
    ("document the CLI flags", "standard"),
    ("debug intermittent authentication failure across services", "complex"),
    ("investigate the root cause of the memory leak", "complex"),
    ("rotate production credentials and update deployment", "critical"),
    ("Refactor the authentication service and add tests", "critical"),
])
def test_examples(task, route):
    assert classify(task)["route"] == route


@pytest.mark.parametrize("task", [
    "fix XSS vulnerability in the comment form",
    "store API keys encrypted at rest",
    "change the login session timeout",
    "update the payment checkout flow",
    "fix session cookie expiry",
    "invalidate user sessions on password reset",
    "log out all sessions",
    "revoke sessions",
    "fix session middleware",
    "update the sessions table",
    "regenerate the session identifier",
])
def test_security_sensitive_is_critical(task):
    result = classify(task)
    assert result["route"] == "critical"
    assert result["recommended_model"] == "opus"


@pytest.mark.parametrize("task", [
    "add a database migration for the orders table",
    "migrate users to the new schema",
    "edit db/migrations/0042_add_index.py",
])
def test_migrations_are_critical(task):
    assert classify(task)["route"] == "critical"


@pytest.mark.parametrize("task", ["add tests for the date parser", "increase test coverage of utils", "write pytest cases"])
def test_test_work_is_standard(task):
    result = classify(task)
    assert (result["route"], result["recommended_model"], result["recommended_effort"]) == ("standard", "sonnet", "medium")


def test_investigating_auth_is_complex():
    result = classify("debug why login fails intermittently")
    assert result["route"] == "complex"


def test_many_files_is_complex():
    assert classify("update a.py, b.py and c.py to use the new logger")["route"] == "complex"


@pytest.mark.parametrize("task", ["", "   \n\t "])
def test_empty_task(task):
    result = classify(task)
    assert result["route"] == "trivial"
    assert "empty" in result["reason"]


def test_unknown_task_defaults_to_standard():
    result = classify("zebra quantum marmalade")
    assert result["route"] == "standard"
    assert result["recommended_effort"] == "medium"


@pytest.mark.parametrize("task", [
    "make the sidebar collapsible", "speed up startup", "delete the old cache",
    "make the sidebar collapsible and commit", "yes, fix the bug in parser.py",
])
def test_other_short_tasks_are_standard(task):
    assert classify(task)["route"] == "standard"


@pytest.mark.parametrize("task", [
    "yes, rm -rf the db", "yes, drop the table", "yes, git reset --hard origin/main",
    "yes, force push", "ok, deploy it", "yes, add a login endpoint",
    "push -f", "push --force", "delete the branch", "delete database", "drop db",
])
def test_critical_signals_override_follow_ups(task):
    assert classify(task)["route"] == "critical"


def test_commit_ordering_is_not_a_git_action():
    assert classify("update the commit ordering algorithm in the scheduler")["route"] == "standard"


def test_output_shape_and_determinism():
    first = classify("add unit tests for parser")
    assert set(first) == {"route", "recommended_model", "recommended_effort", "reason"}
    assert first == classify("add unit tests for parser")


def test_cli_reads_stdin_and_task_file(tmp_path):
    script = str(ROOT / "scripts" / "classify_task.py")
    out = subprocess.run([sys.executable, script], input="fix typo in README", capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["route"] == "trivial"
    task_file = tmp_path / "task.txt"
    task_file.write_text("rotate production credentials")
    out = subprocess.run([sys.executable, script, "--task-file", str(task_file)], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["route"] == "critical"


def test_llm_tokens_are_not_auth():
    assert classify("keep token usage low and split tokens across subscriptions")["route"] != "critical"
    assert classify("refresh the access token on expiry")["route"] == "critical"


@pytest.mark.parametrize("task,route", [
    ("update the plugin to 0.7.0 for all sessions", "standard"),
    ("restart my tmux sessions", "standard"),
])
def test_bare_sessions_is_not_auth(task, route):
    assert classify(task)["route"] == route
