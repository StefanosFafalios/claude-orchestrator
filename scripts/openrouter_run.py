#!/usr/bin/env python3
"""Run a task through an OpenRouter model via the Codex CLI, supervising spend.

Usage: openrouter_run.py <task-file> | --resume RUN_ID | --stop RUN_ID
Exit codes: 0 success, 42 budget/usage limit (prints OPENROUTER_USAGE_LIMIT), 43 paused
over a soft cap (prints OPENROUTER_PAUSED <run-id>), 1 other failure, 2 bad usage.
Env: OPENROUTER_MODEL (required), OPENROUTER_EFFORT, OPENROUTER_SANDBOX (read-only |
workspace-write; default read-only), CODEX_BIN (default codex), OPENROUTER_POLL_SECONDS
(default 30). The API key is read from the environment variable named in the config and
is never placed on the command line: Codex reads it through ``env_key``.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import openrouter_budget as ob
from engine_state import ConfigError, load_config, project_dir, redact

RUN_ID_RE = re.compile(r"^[0-9T]{15}-[0-9a-f]{6}$")
LIMIT_RE = re.compile(r"openrouter_key_limit|openrouter_credits|openrouter_guardrail_budget"
                      r"|openrouter_workspace_budget|Payment Required|\b402\b")
IN_FLIGHT_RE = re.compile(r"openrouter_in_flight_budget")


class UsageError(Exception):
    pass


def poll_seconds() -> float:
    return float(os.environ.get("OPENROUTER_POLL_SECONDS") or 30)


def runs_dir() -> Path:
    return project_dir() / ".claude" / "orchestrator" / "openrouter-runs"


def build_command(config: dict, prompt: str) -> list:
    model = os.environ.get("OPENROUTER_MODEL", "")
    effort = os.environ.get("OPENROUTER_EFFORT", "")
    sandbox = os.environ.get("OPENROUTER_SANDBOX") or "read-only"
    if not model:
        raise UsageError("OPENROUTER_MODEL is not set")
    if sandbox not in ("read-only", "workspace-write"):
        raise UsageError(f"invalid OPENROUTER_SANDBOX: {sandbox}")
    if effort and not re.fullmatch(r"[a-z]+", effort):
        raise UsageError(f"invalid OPENROUTER_EFFORT: {effort}")
    # -c values are TOML; a JSON string is a valid TOML basic string.
    overrides = {
        "model_provider": "openrouter",
        "model_providers.openrouter.name": "openrouter",
        "model_providers.openrouter.base_url": ob.api_base(),
        "model_providers.openrouter.env_key": config["openrouter"]["key_env"],
    }
    if effort:
        overrides["model_reasoning_effort"] = effort
    cmd = [os.environ.get("CODEX_BIN") or "codex", "exec", "--sandbox", sandbox, "--model", model]
    for name, value in overrides.items():
        cmd += ["-c", f"{name}={json.dumps(value)}"]
    return cmd + [prompt]


def refresh(key: str) -> dict | None:
    """Fetch and cache the key's spend; ``None`` when the fetch fails (errors never change the exit)."""
    try:
        data = ob.fetch_key(key)
        ob.write_snapshot(data, time.time())
        return data
    except (OSError, ValueError, http.client.HTTPException):
        return None


def start(run: Path, cmd: list) -> subprocess.Popen:
    for name in ("exit", "out", "err"):
        (run / name).unlink(missing_ok=True)
    script = f'"$0" "$@"; echo $? > {shlex.quote(str(run / "exit"))}'
    with open(run / "out", "w") as out, open(run / "err", "w") as err:
        proc = subprocess.Popen(["sh", "-c", script, *cmd], stdin=subprocess.DEVNULL, stdout=out,
                                stderr=err, start_new_session=True)
    (run / "pid").write_text(str(proc.pid))
    return proc


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists but owned by another user
        pass
    return True


def guard(job: dict) -> None:
    """Codex runs in its own session, so the runner's signals never reach it: forward a kill."""
    def terminate(signum, frame):
        if job.get("pid"):
            try:
                os.killpg(job["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        sys.exit(1)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, terminate)


def supervise(run: Path, key: str, budget: dict, job: dict, alive) -> int:
    """Poll spend while the run is alive; pause the group on an unapproved soft cap."""
    poll = poll_seconds()
    next_check = time.monotonic() + poll
    while alive():
        if time.monotonic() >= next_check:
            next_check = time.monotonic() + poll
            # Only a snapshot fetched now is judged: over_soft_cap ignores observed_at.
            snapshot = refresh(key)
            window = snapshot and ob.over_soft_cap(snapshot, budget)
            if window and window not in job["approved_windows"]:
                os.killpg(job["pid"], signal.SIGSTOP)
                paused = dict(job, window=window, spend=snapshot.get(f"usage_{window}"),
                              cap=budget.get(f"{window}_usd"))
                (run / "paused.json").write_text(json.dumps(paused))
                print(f"OPENROUTER_PAUSED {job['run_id']}")
                return 43
        time.sleep(min(poll, 0.5))
    return finish(run, key, budget, job)


def finish(run: Path, key: str, budget: dict, job: dict) -> int:
    refresh(key)
    out = (run / "out").read_text(errors="replace")
    err = (run / "err").read_text(errors="replace")
    try:
        code = int((run / "exit").read_text())
    except (OSError, ValueError):
        code = 1  # killed before the wrapper could record a status
    if code != 0:
        # Codex echoes the prompt into stderr; only its tail holds the error.
        text = out + "\n".join(err.splitlines()[-20:])
        if LIMIT_RE.search(text):
            print("OPENROUTER_USAGE_LIMIT")
            sys.stderr.write(redact(out + err))
            return 42
        if IN_FLIGHT_RE.search(text) and not job.get("rerun"):
            time.sleep(poll_seconds())
            job["rerun"] = True
            proc = start(run, job["command"])
            job["pid"] = proc.pid
            return supervise(run, key, budget, job, lambda: proc.poll() is None)
        if IN_FLIGHT_RE.search(text) or code in (42, 43):  # 42 and 43 are reserved for this contract
            code = 1
    sys.stdout.write(out)
    sys.stderr.write(redact(err))
    return code


def load_paused(run_id: str) -> tuple:
    if not RUN_ID_RE.match(run_id):
        raise UsageError(f"invalid run id: {run_id}")
    run = runs_dir() / run_id
    try:
        return run, json.loads((run / "paused.json").read_text())
    except (OSError, ValueError) as exc:
        raise UsageError(f"run {run_id} is not paused") from exc


def main(argv: list) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    budget = config["openrouter"]["budget"]
    key_env = config["openrouter"]["key_env"]
    key = os.environ.get(key_env, "")
    try:
        if len(argv) == 2 and argv[0] == "--stop":
            run, job = load_paused(argv[1])
            for sig in (signal.SIGCONT, signal.SIGTERM):  # a stopped process ignores TERM until continued
                try:
                    os.killpg(job["pid"], sig)
                except ProcessLookupError:
                    pass
            (run / "paused.json").unlink()
            print("stopped by user", file=sys.stderr)
            return 1
        if not key:
            print(f"{key_env} is not set", file=sys.stderr)
            return 1
        if len(argv) == 2 and argv[0] == "--resume":
            run, job = load_paused(argv[1])
            job["approved_windows"] = job["approved_windows"] + [job.pop("window")]
            (run / "paused.json").unlink()
            guard(job)
            os.killpg(job["pid"], signal.SIGCONT)
            return supervise(run, key, budget, job, lambda: pid_alive(job["pid"]))
        if len(argv) != 1 or not os.path.isfile(argv[0]):
            raise UsageError("usage: openrouter_run.py <task-file> | --resume RUN_ID | --stop RUN_ID")
        task_file = os.path.abspath(argv[0])
        cmd = build_command(config, Path(task_file).read_text())
    except UsageError as exc:
        print(exc, file=sys.stderr)
        return 2
    refresh(key)
    snapshot = ob.read_snapshot()  # cap_spent is window-aware, so a stale cache is safe here
    if snapshot and ob.cap_spent(snapshot, budget, time.time()):
        print("OPENROUTER_USAGE_LIMIT")
        return 42
    run_id = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3)
    run = runs_dir() / run_id
    run.mkdir(parents=True)
    job = {"run_id": run_id, "pid": None, "task_file": task_file, "approved_windows": [], "command": cmd}
    guard(job)
    proc = start(run, cmd)
    job["pid"] = proc.pid
    return supervise(run, key, budget, job, lambda: proc.poll() is None)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
