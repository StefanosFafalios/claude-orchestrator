#!/usr/bin/env python3
"""Write a resumable handoff to ``.claude/orchestrator/handoffs/<task-id>.md``.

Text arguments starting with ``@`` are read from that file (``@-`` reads stdin),
so long completed-work notes or test output need no shell quoting.
The handoff path is printed on stdout.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from engine_state import PLUGIN_ROOT, ConfigError, load_config, project_dir, redact

TEMPLATE = PLUGIN_ROOT / "templates" / "handoff.md"
MAX_DIFF_CHARS = 60_000
MAX_TEST_CHARS = 8_000
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def read_arg(value: str | None) -> str:
    if not value:
        return ""
    if value == "@-":
        return sys.stdin.read()
    if value.startswith("@"):
        return Path(value[1:]).read_text(encoding="utf-8")
    return value


def git_diff(root: Path) -> str:
    """Staged and unstaged changes against HEAD, plus untracked file names."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)

    diff = git("diff", "HEAD")
    if diff.returncode != 0:  # No commits yet, or not a repository.
        diff = git("diff")
        if diff.returncode != 0:
            return "(not a git repository)"
    text = diff.stdout
    untracked = git("ls-files", "--others", "--exclude-standard").stdout.split()
    if untracked:
        text += "\n# untracked files:\n" + "\n".join(f"#   {name}" for name in untracked)
    return text.strip() or "(no changes)"


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} characters]"


def render(fields: dict) -> str:
    template = TEMPLATE.read_text(encoding="utf-8")
    return re.sub(r"\{\{(\w+)\}\}", lambda m: fields.get(m.group(1), ""), template)


def build_fields(args: argparse.Namespace, config: dict, root: Path) -> dict:
    commands = config.get("commands", {})
    files = args.relevant_file or []
    completed = read_arg(args.completed_work).strip() or "(none recorded)"
    return {
        "task_id": args.task_id,
        "objective": read_arg(args.objective).strip(),
        "task_route": args.task_route,
        "source_engine": args.source_engine,
        "target_engine": args.target_engine,
        "completed_work": redact(completed),
        "remaining_work": redact(read_arg(args.remaining_work).strip() or "(entire objective)"),
        "relevant_files": "\n".join(f"- `{f}`" for f in files) or "(not identified)",
        "git_diff": redact(truncate(git_diff(root), MAX_DIFF_CHARS)),
        "test_output": redact(truncate(read_arg(args.test_output).strip(), MAX_TEST_CHARS)) or "(none)",
        "test_command": commands.get("test") or "(not configured)",
        "lint_command": commands.get("lint") or "(not configured)",
        "typecheck_command": commands.get("typecheck") or "(not configured)",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument("--task-route", choices=("trivial", "standard", "complex", "critical"), required=True)
    parser.add_argument("--source-engine", required=True, help="engine that did (or would have done) the work")
    parser.add_argument("--target-engine", choices=("claude", "codex"), required=True)
    parser.add_argument("--completed-work", default="")
    parser.add_argument("--remaining-work", default="")
    parser.add_argument("--relevant-file", action="append", help="repeatable")
    parser.add_argument("--test-output", default="")
    args = parser.parse_args(argv)

    if not TASK_ID.match(args.task_id):
        print(f"error: unsafe task id {args.task_id!r}", file=sys.stderr)
        return 2
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    root = project_dir()
    out = root / ".claude" / "orchestrator" / "handoffs" / f"{args.task_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(build_fields(args, config, root)), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
