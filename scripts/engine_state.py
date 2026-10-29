#!/usr/bin/env python3
"""Configuration loading and Claude/Codex quota state for the orchestrator.

State lives in ``<project>/.claude/engine-state.json``. The project directory is
``$CLAUDE_PROJECT_DIR`` when set, otherwise the current working directory.
Other plugin scripts import ``load_config``, ``load_state`` and ``effective_status``
from here so there is one implementation of each.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PLUGIN_ROOT / "templates" / "orchestrator.config.json"
ENGINES = ("claude", "codex")
STATUSES = ("healthy", "near_limit", "limited")
MAX_ERROR_CHARS = 500

DEFAULT_ENGINE = {
    "status": "healthy",
    "remaining_pct": 100,
    "last_error": None,
    "last_checked": None,
}

# Credential-shaped substrings scrubbed from anything persisted or handed off.
SECRET_PATTERNS = [
    re.compile(r"(?i)\b(bearer|token|api[_-]?key|secret|password|passwd)\b(\s*[:=]\s*|\s+)\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]


class ConfigError(Exception):
    """Raised when a configuration file cannot be used."""


def redact(text: str) -> str:
    """Replace credential-shaped substrings with a marker."""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def project_dir() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def config_path(root: Path | None = None) -> Path:
    return (root or project_dir()) / ".claude" / "orchestrator.config.json"


def state_path(root: Path | None = None) -> Path:
    return (root or project_dir()) / ".claude" / "engine-state.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def deep_merge(base: dict, override: dict) -> dict:
    """Return ``base`` updated recursively by ``override``; lists are replaced."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path} (line {exc.lineno}, column {exc.colno}): {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return data


def load_config(root: Path | None = None) -> dict:
    """Plugin defaults overlaid by the project's ``.claude/orchestrator.config.json``."""
    config = _read_json(DEFAULT_CONFIG_PATH)
    path = config_path(root)
    if path.is_file():
        config = deep_merge(config, _read_json(path))
    config.setdefault("automatic", True)
    config.setdefault("usage_sync", True)
    return config


def load_state(root: Path | None = None) -> dict:
    """Current state with defaults filled in; a corrupt file is treated as defaults."""
    state = {engine: dict(DEFAULT_ENGINE) for engine in ENGINES}
    path = state_path(root)
    if path.is_file():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            saved = {}
        for engine in ENGINES:
            if isinstance(saved.get(engine), dict):
                state[engine].update(saved[engine])
    return state


def save_state(state: dict, root: Path | None = None) -> None:
    """Write state atomically: temporary file in the same directory, then rename."""
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".engine-state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _epoch(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except ValueError:
        return None


def sync_usage(state: dict, config: dict, observations: dict | None = None) -> bool:
    """Apply observed usage (see usage_sources.py) to ``state``; return True if it changed.

    An observation applies when it is at least as new as the entry's last check, or when
    the entry is ``limited`` and every observed window has since reset.
    """
    if config.get("usage_sync") is False:
        return False
    if observations is None:
        from usage_sources import observe  # Imported lazily: only syncing reads ~/.claude and ~/.codex.
        observations = observe()
    changed = False
    for engine in ENGINES:
        obs = observations.get(engine)
        if not obs:
            continue
        entry = state[engine]
        last = _epoch(entry.get("last_checked"))
        fresh = last is None or obs["observed_at"] >= last
        if not (fresh or (entry.get("status") == "limited" and obs["windows_reset"])):
            continue
        remaining = obs["remaining_pct"]
        if remaining <= 0:
            status = "limited"
        elif remaining < config["thresholds"]["near_limit_pct"]:
            status = "near_limit"
        else:
            status = "healthy"
        update = {
            "status": status,
            "remaining_pct": remaining,
            "last_checked": datetime.fromtimestamp(max(obs["observed_at"], last or 0), timezone.utc)
            .replace(microsecond=0).isoformat(),
            "usage_source": obs["source"],
            "resets_at": obs["resets_at"],
        }
        if status != "limited":
            update["last_error"] = None
        if any(entry.get(key) != value for key, value in update.items()):
            entry.update(update)
            changed = True
    return changed


def load_synced_state(config: dict, root: Path | None = None) -> dict:
    """State with fresh observed usage applied and saved."""
    state = load_state(root)
    if sync_usage(state, config):
        save_state(state, root)
    return state


def effective_status(entry: dict, config: dict) -> str:
    """``limited`` wins; otherwise a remaining percentage below the threshold is ``near_limit``."""
    if entry.get("status") == "limited":
        return "limited"
    if entry.get("status") == "near_limit":
        return "near_limit"
    if float(entry.get("remaining_pct", 100)) < config["thresholds"]["near_limit_pct"]:
        return "near_limit"
    return "healthy"


def cmd_set(state: dict, config: dict, args: argparse.Namespace) -> None:
    entry = state[args.engine]
    entry["remaining_pct"] = args.remaining_pct
    if args.status:
        entry["status"] = args.status
    else:
        below = args.remaining_pct < config["thresholds"]["near_limit_pct"]
        entry["status"] = "near_limit" if below else "healthy"
    entry["last_checked"] = now_iso()


def cmd_mark_limit(state: dict, args: argparse.Namespace) -> None:
    entry = state[args.engine]
    entry.update(
        status="limited",
        remaining_pct=0,
        last_error=redact(args.message)[:MAX_ERROR_CHARS],
        last_checked=now_iso(),
    )


def cmd_record_result(state: dict, args: argparse.Namespace) -> None:
    entry = state[args.engine]
    entry["last_result"] = {"task_id": args.task_id, "status": args.status, "at": now_iso()}
    entry["last_checked"] = now_iso()
    if args.status == "success" and entry.get("status") == "limited":
        # A successful run proves the quota has reset.
        entry.update(status="healthy", remaining_pct=100, last_error=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="sync observed usage, then print current state as JSON")
    sub.add_parser("sync", help="apply observed Claude/Codex usage from local files")
    sub.add_parser("config", help="print the merged configuration as JSON")
    sub.add_parser("reset", help="restore default state")

    set_p = sub.add_parser("set", help="set an engine's status and remaining percentage")
    set_p.add_argument("--engine", choices=ENGINES, required=True)
    set_p.add_argument("--status", choices=STATUSES, help="derived from --remaining-pct when omitted")
    set_p.add_argument("--remaining-pct", type=float, required=True)

    limit_p = sub.add_parser("mark-limit", help="mark an engine as having hit its usage limit")
    limit_p.add_argument("--engine", choices=ENGINES, required=True)
    limit_p.add_argument("--message", default="usage limit reported")

    result_p = sub.add_parser("record-result", help="record a task outcome for an engine")
    result_p.add_argument("--engine", choices=ENGINES, required=True)
    result_p.add_argument("--task-id", required=True)
    result_p.add_argument("--status", choices=("success", "failed"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "config":
        print(json.dumps(config, indent=2))
        return 0
    if args.command == "reset":
        state = {engine: dict(DEFAULT_ENGINE) for engine in ENGINES}
    elif args.command in ("status", "sync"):
        print(json.dumps(load_synced_state(config), indent=2))
        return 0
    else:
        state = load_state()
        if args.command == "set":
            cmd_set(state, config, args)
        elif args.command == "mark-limit":
            cmd_mark_limit(state, args)
        elif args.command == "record-result":
            cmd_record_result(state, args)
    if args.command != "status":
        save_state(state)
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
