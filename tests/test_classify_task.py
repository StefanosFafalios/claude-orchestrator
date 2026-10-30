import json
import subprocess
import sys

import pytest
from conftest import ROOT

from classify_task import classify, is_follow_up


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
])
def test_security_sensitive_is_critical(task):
    result = classify(task)
    assert result["route"] == "critical"
    assert result["recommended_model"] == "opus"
    assert result["requires_codex_review"] is True


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


def test_investigating_auth_is_complex_with_codex_review():
    result = classify("debug why login fails intermittently")
    assert result["route"] == "complex"
    assert result["requires_codex_review"] is True


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
    "yes", "ok", "yes, ignore it and push", "commit it", "lgtm",
])
def test_short_follow_ups_without_context_are_standard(task):
    assert is_follow_up(task)
    assert classify(task)["route"] == "standard"


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


def test_short_follow_up_word_limit():
    assert is_follow_up("yes one two three four five six seven")
    assert not is_follow_up("yes one two three four five six seven eight")


def test_follow_up_uses_last_paragraph():
    result = classify("yes", "Earlier: implement a feature.\n\nShould I bump the version, then commit and push?")
    assert result["route"] == "trivial"
    assert result["reason"].startswith("follow-up to: ")
    assert classify("yes", "Want me to implement the retry queue across services?")["route"] == "complex"
    assert classify("yes")["reason"] == "follow-up without context; defaulting to standard"
    assert classify("yes, force push", "Should I commit?")["route"] == "critical"
    assert classify("commit and push the changes after fixing the issue now please today")["route"] == "standard"


@pytest.mark.parametrize("suffix", [
    "| Command | Status |\n| --- | --- |\n| rm -rf db | quoted |",
    "- First item\n- Second item",
])
def test_follow_up_uses_offer_before_table_or_list(suffix):
    reply = "Want me to implement the retry queue across services?\n\n" + suffix
    assert classify("yes", reply)["route"] == "complex"


def test_commit_ordering_is_not_a_git_action():
    assert classify("update the commit ordering algorithm in the scheduler")["route"] == "standard"


def test_output_shape_and_determinism():
    first = classify("add unit tests for parser")
    assert set(first) == {"route", "recommended_model", "recommended_effort", "reason", "requires_codex_review"}
    assert first == classify("add unit tests for parser")


def test_cli_reads_stdin_and_task_file(tmp_path):
    script = str(ROOT / "scripts" / "classify_task.py")
    out = subprocess.run([sys.executable, script], input="fix typo in README", capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["route"] == "trivial"
    task_file = tmp_path / "task.txt"
    task_file.write_text("rotate production credentials")
    out = subprocess.run([sys.executable, script, "--task-file", str(task_file)], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["route"] == "critical"
    reply_file = tmp_path / "reply.txt"
    reply_file.write_text("Should I commit and push?")
    out = subprocess.run([sys.executable, script, "--replying-to-file", str(reply_file)],
                         input="yes", capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["route"] == "trivial"


def test_llm_tokens_are_not_auth():
    assert classify("keep token usage low and split tokens across subscriptions")["route"] != "critical"
    assert classify("refresh the access token on expiry")["route"] == "critical"
