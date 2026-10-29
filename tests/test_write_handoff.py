import subprocess

from write_handoff import main


def git(project, *args):
    subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)


def test_handoff_contains_contract_and_diff(project, write_config, capsys):
    write_config({"commands": {"test": "pytest -q", "lint": "ruff check ."}})
    git(project, "init", "-q")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    (project / "app.py").write_text("print('hi')\n")
    git(project, "add", "app.py")
    notes = project / "done.txt"
    notes.write_text("Added retry decorator to webhook client")

    rc = main([
        "--task-id", "retry-1", "--objective", "Implement webhook retries", "--task-route", "standard",
        "--source-engine", "claude", "--target-engine", "codex",
        "--completed-work", f"@{notes}", "--remaining-work", "Add tests for retry backoff",
        "--relevant-file", "app.py", "--test-output", "FAILED test_retry - token=abc123",
    ])
    assert rc == 0
    path = project / ".claude" / "orchestrator" / "handoffs" / "retry-1.md"
    assert capsys.readouterr().out.strip() == str(path)
    text = path.read_text()
    for needle in (
        "## Original objective\nImplement webhook retries",
        "## Task route\nstandard",
        "## Implementing engine\nclaude",
        "## Remaining work\nAdd tests for retry backoff",
        "- `app.py`",
        "+print('hi')",
        "- Test: pytest -q",
        "- Typecheck: (not configured)",
        "## Output contract",
        "## Do not redo\nAdded retry decorator to webhook client",
        "Co-Authored-By",
    ):
        assert needle in text
    assert "abc123" not in text
    assert "{{" not in text


def test_handoff_outside_git_and_bad_task_id(project):
    rc = main(["--task-id", "t", "--objective", "x", "--task-route", "trivial", "--source-engine", "codex", "--target-engine", "claude"])
    assert rc == 0
    text = (project / ".claude" / "orchestrator" / "handoffs" / "t.md").read_text()
    assert "(not a git repository)" in text
    assert "## Do not redo\n(none recorded)" in text
    assert main(["--task-id", "../escape", "--objective", "x", "--task-route", "trivial",
                 "--source-engine", "codex", "--target-engine", "claude"]) == 2
