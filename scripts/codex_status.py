#!/usr/bin/env python3
"""Show recent Codex rollout activity in the Claude Code status line."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time


def events(data):
    for line in data.splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                yield item
        except (ValueError, UnicodeDecodeError):
            continue


def activity(item):
    payload = item.get("payload", {})
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if item.get("type") == "event_msg":
        if kind in ("task_complete", "turn_aborted"):
            return "done"
        if kind in ("task_started", "agent_reasoning", "agent_message", "exec_command_end", "patch_apply_end"):
            return "thinking"
        if kind == "exec_command_begin":
            command = payload.get("command", "")
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            return "running " + " ".join(str(command).split())[:40]
        if kind == "patch_apply_begin":
            changes = payload.get("changes", {})
            if isinstance(changes, dict) and changes:
                return "editing " + ", ".join(changes)[:80]
    if item.get("type") == "response_item":
        name = payload.get("name", "")
        if kind in ("function_call", "custom_tool_call"):
            raw = payload.get("arguments", payload.get("input", ""))
            if name == "exec":
                files = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", str(raw), re.MULTILINE)
                if files:
                    return "editing " + ", ".join(files)[:80]
                match = re.search(r'cmd:\s*"((?:[^"\\]|\\.)*)"', str(raw))
                if match:
                    try:
                        command = json.loads('"' + match.group(1) + '"')
                    except ValueError:
                        command = match.group(1)
                    return "running " + " ".join(command.split())[:40]
            if name in ("apply_patch", "functions.apply_patch"):
                files = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", str(raw), re.MULTILINE)
                return "editing " + ", ".join(files)[:80] if files else "editing"
            if name in ("exec_command", "functions.exec_command", "shell", "container.exec"):
                try:
                    args = json.loads(raw) if isinstance(raw, str) else raw
                    command = args.get("cmd", args.get("command", ""))
                    if isinstance(command, list):
                        command = " ".join(command)
                    return "running " + " ".join(command.split())[:40]
                except (ValueError, TypeError, AttributeError):
                    return "thinking"
        if kind in ("function_call_output", "custom_tool_call_output", "reasoning"):
            return "thinking"
    return None


def main():
    data = json.load(sys.stdin)
    project = data.get("workspace", {}).get("current_dir") or data.get("cwd")
    if not project:
        return
    project = os.path.realpath(project)
    now = time.time()
    candidates = []
    sessions = Path.home() / ".codex" / "sessions"
    directories = sorted((p for p in sessions.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]")
                          if p.is_dir()), reverse=True)[:7]
    for directory in directories:
        for path in directory.glob("rollout-*.jsonl"):
            try:
                mtime = path.stat().st_mtime
                if now - 15 <= mtime <= now + 1:
                    candidates.append((mtime, path))
            except OSError:
                continue
    for _, path in sorted(candidates, reverse=True):
        try:
            with path.open("rb") as handle:
                head = handle.read(262144)
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 65536))
                tail = handle.read()
            head_events = list(events(head))
            tail_events = list(events(tail))
            contexts = [item.get("payload", {}) for item in head_events + tail_events
                        if item.get("type") == "turn_context"]
            if not contexts:
                continue
            cwd = os.path.realpath(contexts[-1].get("cwd", ""))
            if os.path.commonpath((project, cwd)) != project:
                continue
            for item in reversed(tail_events):
                result = activity(item)
                if result == "done":
                    return
                if result:
                    print(" Codex ▸ " + result)
                    return
        except (OSError, ValueError, TypeError, AttributeError):
            continue


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
