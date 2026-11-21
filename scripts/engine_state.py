#!/usr/bin/env python3
"""Configuration loading and Claude/Codex quota state for the orchestrator.

State lives in ``<project>/.claude/engine-state.json``. The project directory is
``$CLAUDE_PROJECT_DIR`` when set, otherwise the current working directory.
Other plugin scripts import ``load_config``, ``load_state`` and ``effective_status``
from here so there is one implementation of each.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no locking, same as before
    fcntl = None

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PLUGIN_ROOT / "templates" / "orchestrator.config.json"
ENGINES = ("claude", "codex", "openrouter")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
STATUSES = ("healthy", "near_limit", "limited", "unavailable")
LOGGED_OUT_RE = re.compile(r"not logged in|log ?in required|logged_out", re.I)
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


# Code-review text: keep ``if token != expected:`` readable; scrub only credential-looking values.
VALUE_PATTERNS = SECRET_PATTERNS[1:] + [
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"),  # JWT: its dotted form looks like code
]
# A keyword assigned a literal: quoted, or bare like a .env line. A closing quote on the key covers JSON/YAML;
# a bare call, index or short dotted name (``make_token()``, ``request.form``) is code, not a secret.
ASSIGNED_SECRET = re.compile(r"""(?i)(?<![A-Za-z0-9])((?:token|api[_-]?key|secret|password|passwd)(?:_\w*)?)\b(["']?\s*[:=]\s*)"""
                             r"""(?:(["'])[^"'\n]{6,}\3|(?!(?:[A-Za-z_]\w{0,19}\.)+[A-Za-z_]\w{0,19}(?=\s|$|[,;]))"""
                             r"""[^\s"'()\[\]{}]{6,}(?=\s|$|[,;]))""")


def redact(text: str, values_only: bool = False) -> str:
    """Replace credential-shaped substrings with a marker; ``values_only`` spares code that merely names a secret."""
    if values_only:
        for pattern in VALUE_PATTERNS:
            text = pattern.sub("[REDACTED]", text)
        return ASSIGNED_SECRET.sub(r"\1\2[REDACTED]", text)
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
    import model_map  # imported here: model_map imports ConfigError from this module

    # Tests point this at a frozen capability map so a re-seeded template does not break them.
    config = _read_json(Path(os.environ.get("ORCHESTRATOR_DEFAULT_CONFIG") or DEFAULT_CONFIG_PATH))
    path = config_path(root)
    project_raw: dict = {}
    if path.is_file():
        project_raw = _read_json(path)
        config = deep_merge(config, project_raw)
    config["legacy_keys"] = model_map.legacy_keys(project_raw)
    config.pop("defaults", None)  # legacy keys are reported above and ignored
    config.get("codex", {}).pop("tiers", None)
    config.get("codex", {}).pop("default_model", None)
    model_map.validate(config)
    config.setdefault("automatic", True)
    config.setdefault("usage_sync", True)
    return config


def default_state() -> dict:
    state = {engine: dict(DEFAULT_ENGINE) for engine in ENGINES}
    state["supercritical_efforts"] = {}
    return state


def engine_enabled(config: dict, engine: str) -> bool:
    """Enabled in config; OpenRouter is off by default and also needs its API key in the environment."""
    if not config.get("engines", {}).get(engine, engine != "openrouter"):
        return False
    if engine == "openrouter":
        return bool(os.environ.get(config["openrouter"]["key_env"]))
    return True


def load_state(root: Path | None = None) -> dict:
    """Current state with defaults filled in; a corrupt file is treated as defaults."""
    state = default_state()
    path = state_path(root)
    if path.is_file():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            saved = {}
        for engine in ENGINES:
            if isinstance(saved.get(engine), dict):
                state[engine].update(saved[engine])
        if isinstance(saved.get("supercritical_efforts"), dict):
            state["supercritical_efforts"] = saved["supercritical_efforts"]
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
        observations = observe(config)
    changed = False
    for engine in ENGINES:
        obs = observations.get(engine)
        if not obs:
            continue
        entry = state[engine]
        last = _epoch(entry.get("last_checked"))
        fresh = last is None or obs["observed_at"] >= last
        if entry.get("status") == "unavailable":  # quota data says nothing about login or model support
            continue
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


@contextlib.contextmanager
def state_lock(root: Path | None = None, wait: float = 2.0):
    """Exclusive lock around a read-change-write of the state file; never hold it across a subprocess or network call.

    Best effort: the prompt hook must finish within 5 s and never fail, so an unwritable project or a
    holder stuck longer than ``wait`` seconds (e.g. stopped with Ctrl-Z) means running unlocked, as before.
    """
    handle = None
    if fcntl is not None:
        path = state_path(root).parent / "orchestrator" / "engine-state.lock"  # setup git-ignores .claude/orchestrator/
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "a")
        except OSError:
            handle = None
    try:
        deadline = time.monotonic() + wait
        while handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:  # e.g. ENOLCK on NFS: run unlocked rather than fail the hook
                if not isinstance(exc, BlockingIOError) or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        yield
    finally:
        if handle:
            handle.close()  # releases the lock


def load_synced_state(config: dict, root: Path | None = None) -> dict:
    """State with fresh observed usage applied and saved."""
    with state_lock(root):
        state = load_state(root)
        if sync_usage(state, config):
            save_state(state, root)
    return state


def effective_status(entry: dict, config: dict) -> str:
    """``unavailable`` and ``limited`` win; otherwise a remaining percentage below the threshold is ``near_limit``."""
    if entry.get("status") in ("unavailable", "limited"):
        return entry["status"]
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


def cmd_mark_unavailable(state: dict, args: argparse.Namespace) -> None:
    """Logged out or no supported model: not a quota limit, so it lasts until a successful run or a reset."""
    state[args.engine].update(status="unavailable", last_error=redact(args.message)[:MAX_ERROR_CHARS],
                              last_checked=now_iso())


def check_codex_login(state: dict, config: dict) -> bool:
    """Mark Codex unavailable when ``codex login status`` says logged out, and lift a login-caused mark
    when it says logged in. Commands only: hooks must never call codex. Returns whether state changed."""
    codex = os.environ.get("CODEX_BIN") or "codex"
    if not engine_enabled(config, "codex") or not shutil.which(codex):
        return False
    try:
        result = subprocess.run([codex, "login", "status"], capture_output=True, text=True, timeout=10,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    entry = state["codex"]
    if LOGGED_OUT_RE.search(result.stdout + result.stderr):
        if entry.get("status") in ("unavailable", "limited"):  # limited already routes away; keep its reset time
            return False
        entry.update(status="unavailable", last_error="codex: not logged in (run codex login)", last_checked=now_iso())
        return True
    if result.returncode == 0 and entry.get("status") == "unavailable" and LOGGED_OUT_RE.search(entry.get("last_error") or ""):
        entry.update(status="healthy", last_error=None, last_checked=now_iso())
        return True
    return False


def _version(text: str) -> tuple | None:
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(part) for part in found.groups()) if found else None


def latest_codex_version() -> str | None:
    """Latest published Codex CLI version from the public npm registry; None when unreachable."""
    try:
        with urllib.request.urlopen("https://registry.npmjs.org/@openai/codex/latest", timeout=5) as response:
            return json.loads(response.read().decode("utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None


def codex_version_warning(config: dict) -> str | None:
    """Warn when the installed Codex CLI is older than the latest release (an old CLI can fail to sign in)."""
    codex = os.environ.get("CODEX_BIN") or "codex"
    if not engine_enabled(config, "codex") or not shutil.which(codex):
        return None
    try:
        installed = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=10,
                                   stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    latest = latest_codex_version()
    if _version(installed) and _version(latest) and _version(installed) < _version(latest):
        return (f"warning: Codex CLI {'.'.join(map(str, _version(installed)))} is older than the latest {latest}; "
                "update with: npm install -g @openai/codex@latest")
    return None


def cmd_record_result(state: dict, args: argparse.Namespace) -> None:
    entry = state[args.engine]
    entry["last_result"] = {"task_id": args.task_id, "status": args.status, "at": now_iso()}
    entry["last_checked"] = now_iso()
    if args.status == "success" and entry.get("status") == "limited":
        # A successful run proves the quota has reset.
        entry.update(status="healthy", remaining_pct=100, last_error=None)
    elif args.status == "success" and entry.get("status") == "unavailable":
        entry.update(status="healthy", last_error=None)  # it runs again: logged in, model supported


def set_engine_enabled(engine: str, enabled: bool, root: Path | None = None) -> Path:
    """Write ``engines.<engine>`` into the project config atomically, keeping every other key."""
    path = config_path(root)
    try:
        raw = json.loads(path.read_text()) if path.exists() else {}
    except ValueError as exc:
        raise ConfigError(f"{path}: invalid JSON ({exc}); not overwriting it") from exc
    if not isinstance(raw, dict) or not isinstance(raw.setdefault("engines", {}), dict):
        raise ConfigError(f"{path}: the config and its \"engines\" must be JSON objects; not overwriting it")
    raw["engines"][engine] = enabled
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".orchestrator.config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(raw, indent=2) + "\n")
        os.chmod(tmp, path.stat().st_mode if path.exists() else 0o644)  # mkstemp creates 0600
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    status_p = sub.add_parser("status", help="sync observed usage, then print current state as JSON")
    status_p.add_argument("--check-login", action="store_true",
                          help="also run `codex login status` and compare the Codex CLI with the latest release "
                               "(slow, uses the network; for the status and setup commands only)")
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

    unavailable_p = sub.add_parser("mark-unavailable", help="mark an engine unavailable (logged out, model unsupported)")
    unavailable_p.add_argument("--engine", choices=ENGINES, required=True)
    unavailable_p.add_argument("--message", default="engine unavailable")

    result_p = sub.add_parser("record-result", help="record a task outcome for an engine")
    result_p.add_argument("--engine", choices=ENGINES, required=True)
    result_p.add_argument("--task-id", required=True)
    result_p.add_argument("--status", choices=("success", "failed"), required=True)

    for name in ("enable", "disable"):
        sub.add_parser(name, help=f"{name} an engine in the project config").add_argument("engine", choices=ENGINES)

    effort_p = sub.add_parser("set-effort", help="record the effort chosen for a supercritical task")
    effort_p.add_argument("--task-id", required=True)
    effort_p.add_argument("--effort", choices=EFFORTS, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("enable", "disable"):
        try:
            key_env = load_config()["openrouter"]["key_env"]  # an invalid config is refused, never rewritten
            path = set_engine_enabled(args.engine, args.command == "enable")
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"{args.engine} {args.command}d in {path}")
        if args.engine == "openrouter" and args.command == "enable":
            # Report presence only, never the value.
            print(f"{key_env} is {'set' if os.environ.get(key_env) else 'NOT set: OpenRouter stays unused until it is'}")
        return 0
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "set-effort" and not TASK_ID_RE.match(args.task_id):
        print("error: --task-id must match [A-Za-z0-9._-]{1,64}", file=sys.stderr)
        return 2
    if args.command == "config":
        print(json.dumps(config, indent=2))
        return 0
    if args.command in ("status", "sync"):
        state = load_synced_state(config)
        if getattr(args, "check_login", False):
            # shortcut: the login check (a subprocess, up to 10 s) runs unlocked; a concurrent state write during this manual command can be lost.
            if check_codex_login(state, config):
                save_state(state)
            warning = codex_version_warning(config)
            if warning:
                print(warning, file=sys.stderr)
        print(json.dumps(state, indent=2))
        return 0
    with state_lock():
        state = default_state() if args.command == "reset" else load_state()
        if args.command == "set":
            cmd_set(state, config, args)
        elif args.command == "mark-limit":
            cmd_mark_limit(state, args)
        elif args.command == "mark-unavailable":
            cmd_mark_unavailable(state, args)
        elif args.command == "record-result":
            cmd_record_result(state, args)
        elif args.command == "set-effort":
            state["supercritical_efforts"][args.task_id] = args.effort
        save_state(state)
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
