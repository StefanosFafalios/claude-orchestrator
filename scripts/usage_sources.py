#!/usr/bin/env python3
"""Observed quota usage from local Claude Code and Codex files. No network access.

- Claude: the status line input JSON (``rate_limits.five_hour`` / ``seven_day``),
  saved by the user's status line command to ``~/.claude/statusline-input.json``
  (override with ``ORCHESTRATOR_CLAUDE_USAGE_FILE``).
- Codex: the newest session log under ``$CODEX_HOME/sessions`` (default ``~/.codex``),
  whose events carry ``rate_limits.primary`` / ``secondary``.

Each reader returns ``None`` or an observation:
``{"remaining_pct", "observed_at", "resets_at", "windows_reset", "source"}``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

TAIL_BYTES = 256 * 1024


def claude_usage_file() -> Path:
    return Path(os.environ.get("ORCHESTRATOR_CLAUDE_USAGE_FILE") or Path.home() / ".claude" / "statusline-input.json")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _seconds(value) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return value / 1000 if value > 1e12 else float(value)  # Accept epoch milliseconds too.


def observation(windows: list, observed_at: float, source: str, now: float) -> dict | None:
    """Combine (used_pct, resets_at) windows; a window past its reset counts as unused."""
    known = [(float(used), _seconds(reset)) for used, reset in windows if isinstance(used, (int, float))]
    if not known:
        return None
    active = [(used, reset) for used, reset in known if reset is None or reset > now]
    # The most-used window binds: its reset, not the soonest one, is when quota returns.
    used, resets_at = max(active, default=(0.0, None), key=lambda window: window[0])
    return {
        "remaining_pct": round(max(0.0, 100.0 - used), 1),
        "observed_at": observed_at,
        "resets_at": resets_at,
        "windows_reset": not active,
        "source": source,
    }


def claude_usage(now: float | None = None) -> dict | None:
    path = claude_usage_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mtime = path.stat().st_mtime
    except (OSError, ValueError):
        return None
    limits = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(limits, dict):
        return None
    windows = [(w.get("used_percentage"), w.get("resets_at"))
               for w in (limits.get("five_hour"), limits.get("seven_day")) if isinstance(w, dict)]
    return observation(windows, mtime, "claude-statusline", time.time() if now is None else now)


def _newest_codex_log(home: Path) -> Path | None:
    day_dirs = sorted((home / "sessions").glob("*/*/*"), reverse=True)[:3]  # sessions/YYYY/MM/DD
    logs = [log for day in day_dirs for log in day.glob("*.jsonl")]
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def _find_key(obj, key: str):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    return None


def codex_usage(now: float | None = None) -> dict | None:
    try:
        log = _newest_codex_log(codex_home())
        if log is None:
            return None
        with log.open("rb") as handle:
            handle.seek(max(0, log.stat().st_size - TAIL_BYTES))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
        mtime = log.stat().st_mtime
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            limits = _find_key(json.loads(line), "rate_limits")
        except ValueError:
            continue  # The first line of the tail may be cut mid-record.
        if isinstance(limits, dict):
            windows = [(w.get("used_percent"), w.get("resets_at"))
                       for w in (limits.get("primary"), limits.get("secondary")) if isinstance(w, dict)]
            return observation(windows, mtime, "codex-session-log", time.time() if now is None else now)
    return None


def observe() -> dict:
    return {"claude": claude_usage(), "codex": codex_usage()}


if __name__ == "__main__":
    print(json.dumps(observe(), indent=2))
    sys.exit(0)
