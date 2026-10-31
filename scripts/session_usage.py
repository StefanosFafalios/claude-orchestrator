#!/usr/bin/env python3
"""Report token usage for a Claude session and matching Codex rollouts."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


FIELDS = ("count", "fresh_input", "cache_write", "cache_read", "output")
NUMERIC = (*FIELDS, "total")


def entries(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(entry, dict):
                yield entry


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except ValueError:
        return None


def add(groups, key, values):
    row = groups.setdefault(key, dict.fromkeys(FIELDS, 0))
    for field, value in zip(FIELDS, values):
        row[field] += value


def tokens(usage, field):
    value = usage.get(field, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def collect(transcript, project_dir, codex_home):
    main = list(entries(transcript))
    times = []
    groups = {}
    requests = {}
    sources = [(transcript, main)]
    sources.extend((path, entries(path)) for path in
                   sorted((transcript.parent / transcript.stem / "subagents").glob("agent-*.jsonl")))
    for path, source in sources:
        for entry in source:
            time = timestamp(entry.get("timestamp"))
            if time is not None:
                times.append(time)
            if entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            model = message.get("model")
            if not model or model == "<synthetic>":
                continue
            request_id = entry.get("requestId")
            # Entries without IDs cannot safely be deduplicated.
            key = (path, request_id) if isinstance(request_id, str) and request_id else (path, object())
            requests[key] = (model, entry.get("effort") or "default", message["usage"])
    for model, effort, usage in requests.values():
        add(groups, ("Claude", model, effort), (1, tokens(usage, "input_tokens"),
            tokens(usage, "cache_creation_input_tokens"), tokens(usage, "cache_read_input_tokens"),
            tokens(usage, "output_tokens")))

    if times:
        start, end = min(times), datetime.now(timezone.utc)
        resolved_cwds = {}
        for path in (codex_home / "sessions").rglob("rollout-*.jsonl"):
            if path.stat().st_mtime < start.timestamp():
                continue
            context = None
            previous = None
            counted = set()
            for entry in entries(path):
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue
                if entry.get("type") == "turn_context":
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str):
                        if cwd not in resolved_cwds:
                            resolved_cwds[cwd] = Path(cwd).resolve()
                        resolved = resolved_cwds[cwd]
                        matches = resolved == project_dir or project_dir in resolved.parents
                    else:
                        matches = False
                    context = (payload.get("model") or "unknown",
                               payload.get("effort") or payload.get("reasoning_effort") or "default") if matches else None
                if entry.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                total = info.get("total_token_usage")
                last = info.get("last_token_usage")
                if isinstance(total, dict):
                    fields = ("input_tokens", "cached_input_tokens", "output_tokens")
                    current = tuple(tokens(total, field) for field in fields)
                    delta = tuple(max(0, value - old) for value, old in zip(current, previous)) if previous else current
                    previous = current
                elif isinstance(last, dict):
                    delta = tuple(tokens(last, field) for field in
                                  ("input_tokens", "cached_input_tokens", "output_tokens"))
                    previous = tuple(value + change for value, change in
                                     zip(previous or (0, 0, 0), delta))
                else:
                    continue
                time = timestamp(entry.get("timestamp"))
                if context is None or time is None or not start <= time <= end:
                    continue
                key = ("Codex", *context)
                count = int(key not in counted)
                counted.add(key)
                input_tokens, cached, output = delta
                add(groups, key, (count, max(0, input_tokens - cached), 0, cached, output))
    rows = []
    for (engine, model, effort), values in groups.items():
        rows.append(dict(engine=engine, model=model, effort=effort, **values,
                         total=sum(values[field] for field in FIELDS[1:])))
    return sorted(rows, key=lambda row: (-row["total"], row["engine"], row["model"], row["effort"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        project_dir = args.project_dir.resolve()
        transcript = args.transcript
        if transcript is None:
            encoded = "".join(char if char.isalnum() else "-" for char in str(project_dir))
            directory = Path.home() / ".claude" / "projects" / encoded
            session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
            selected = directory / (session_id + ".jsonl") if session_id else None
            if selected is not None and selected.is_file():
                transcript = selected
            else:
                candidates = list(directory.glob("*.jsonl"))
                if not candidates:
                    parser.error("no session transcript found in {}".format(directory))
                transcript = max(candidates, key=lambda path: path.stat().st_mtime)
        rows = collect(transcript, project_dir, Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    except OSError as exc:
        parser.exit(1, "session-usage: {}\n".format(exc))
    totals = {field: sum(row[field] for row in rows) for field in NUMERIC}
    if args.json:
        print(json.dumps({"groups": rows, "total": totals}, indent=2))
        return 0
    headers = ("Engine", "Model", "Effort", "Requests/runs", "Fresh input", "Cache write", "Cache read", "Output", "Total")
    data = [[row["engine"], row["model"], row["effort"], *[format(row[field], ",") for field in NUMERIC]] for row in rows]
    data.append(["TOTAL", "", "", *[format(totals[field], ",") for field in NUMERIC]])
    widths = [max(len(str(row[i])) for row in [headers, *data]) for i in range(len(headers))]
    for row in [headers, *data]:
        print("  ".join(str(value).ljust(widths[i]) if i < 3 else str(value).rjust(widths[i]) for i, value in enumerate(row)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
