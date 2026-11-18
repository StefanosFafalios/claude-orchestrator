#!/usr/bin/env python3
"""OpenRouter spend pacing and the account-level key/model caches.

The key snapshot (``GET /key``) and model list (``GET /models``) are cached under
``~/.cache/cost-aware-orchestrator`` and shared by all projects. Hooks read only the
caches; ``verify`` and ``refresh`` are the only network entry points. Windows are UTC.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "cost-aware-orchestrator"
SNAPSHOT_FIELDS = ("usage_daily", "usage_weekly", "usage_monthly", "limit", "limit_remaining", "limit_reset")


def caps(yearly_usd: float) -> dict:
    """Monthly is an even twelfth; weekly and daily allow 2x and 3x their even share."""
    return {
        "yearly_usd": yearly_usd,
        "monthly_usd": round(yearly_usd / 12, 2),
        "weekly_usd": round(yearly_usd / 52 * 2, 2),
        "daily_usd": round(yearly_usd / 365 * 3, 2),
    }


def api_base() -> str:
    return (os.environ.get("OPENROUTER_API_BASE") or "https://openrouter.ai/api/v1").rstrip("/")


def usage_file() -> Path:
    return Path(os.environ.get("ORCHESTRATOR_OPENROUTER_USAGE_FILE") or CACHE_DIR / "openrouter-key.json")


def models_file() -> Path:
    return Path(os.environ.get("ORCHESTRATOR_OPENROUTER_MODELS_FILE") or CACHE_DIR / "openrouter-models.json")


def _get(path: str, timeout: float, key: str | None = None):
    request = urllib.request.Request(api_base() + path)
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    data = body.get("data") if isinstance(body, dict) else None
    if data is None:
        raise ValueError(f"unexpected response from {path}")
    return data


def fetch_key(key: str, timeout: float = 10) -> dict:
    data = _get("/key", timeout, key)
    if not isinstance(data, dict):
        raise ValueError("unexpected /key response")
    return data


def fetch_models(timeout: float = 30) -> list:
    data = _get("/models", timeout)
    if not isinstance(data, list):
        raise ValueError("unexpected /models response")
    return data


def _write_json(path: Path, payload) -> None:
    """Atomic write: temporary file in the same directory, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".openrouter.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_snapshot(data: dict, now: float) -> None:
    """Keep only the spend fields; the key and its label are never stored."""
    snapshot = {name: data.get(name) for name in SNAPSHOT_FIELDS}
    snapshot["observed_at"] = now
    _write_json(usage_file(), snapshot)


def read_snapshot() -> dict | None:
    try:
        data = json.loads(usage_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("observed_at"), (int, float)) else None


def _num(value) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def windows(snapshot: dict, budget: dict, now: float) -> list:
    """``(used_pct, resets_at)`` for day, week and month, then the monthly pace window."""
    t = datetime.fromtimestamp(now, timezone.utc)
    day = t.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)
    days_in_month = calendar.monthrange(t.year, t.month)[1]
    observed = snapshot["observed_at"]
    out = []
    for field, cap_name, start, end in (
        ("usage_daily", "daily_usd", day, day + timedelta(days=1)),
        ("usage_weekly", "weekly_usd", week, week + timedelta(days=7)),
        ("usage_monthly", "monthly_usd", month, month + timedelta(days=days_in_month)),
    ):
        cap = _num(budget.get(cap_name))
        if cap > 0:
            # Spend observed before this window began belongs to the previous one.
            spent = _num(snapshot.get(field)) if observed >= start.timestamp() else 0.0
            out.append((100 * spent / cap, end.timestamp()))
    monthly = _num(budget.get("monthly_usd"))
    if monthly > 0:
        # Spending ahead of the calendar reads as near limit before the hard cap bites.
        elapsed = (now - month.timestamp()) / (days_in_month * 86400)
        allowance = monthly * min(1.0, elapsed + 1 / days_in_month)
        spent = _num(snapshot.get("usage_monthly")) if observed >= month.timestamp() else 0.0
        out.append((min(99.0, 100 * spent / allowance), None))
    return out


def over_soft_cap(snapshot: dict, budget: dict) -> str | None:
    for name, field in (("daily", "usage_daily"), ("weekly", "usage_weekly")):
        cap = _num(budget.get(f"{name}_usd"))
        if cap > 0 and _num(snapshot.get(field)) >= cap:
            return name
    return None


def cap_spent(snapshot: dict, budget: dict, now: float) -> bool:
    """A day/week/month window is used up, or the key's own limit is exhausted."""
    remaining = snapshot.get("limit_remaining")
    if isinstance(remaining, (int, float)) and remaining <= 0:
        return True
    # The pace window has no reset time and is capped at 99, so it never counts as spent.
    return any(used >= 100 for used, reset in windows(snapshot, budget, now) if reset is not None)


def _key(config: dict) -> str:
    return os.environ.get(config["openrouter"]["key_env"], "")


def _cmd_verify(config: dict) -> int:
    key = _key(config)
    if not key:
        print(json.dumps({"key_valid": False, "error": "API key environment variable is not set"}))
        return 1
    try:
        data = fetch_key(key)
    except (OSError, ValueError) as exc:  # HTTPError and URLError are OSErrors
        print(json.dumps({"key_valid": False, "error": str(exc)}))
        return 1
    write_snapshot(data, time.time())
    monthly = config["openrouter"]["budget"].get("monthly_usd")
    print(json.dumps({
        "key_valid": True,
        "limit": data.get("limit"),
        "limit_reset": data.get("limit_reset"),
        "monthly_cap": monthly,
        "matches": data.get("limit") == monthly and data.get("limit_reset") == "monthly",
        "guardrail_budgets": data.get("budgets"),
    }))
    return 0


def _cmd_refresh(config: dict) -> int:
    key = _key(config)
    try:
        if key:
            write_snapshot(fetch_key(key), time.time())
        models = [m["id"] for m in fetch_models() if isinstance(m, dict) and isinstance(m.get("id"), str)]
        _write_json(models_file(), {"models": models})
    except (OSError, ValueError) as exc:
        print(f"refresh failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    caps_parser = sub.add_parser("caps", help="print the monthly/weekly/daily caps for a yearly budget")
    caps_parser.add_argument("--yearly-usd", type=float, required=True)
    sub.add_parser("verify", help="check the API key and cache its spend")
    sub.add_parser("refresh", help="refresh the key and model caches")
    args = parser.parse_args(argv)
    if args.command == "caps":
        print(json.dumps(caps(args.yearly_usd)))
        return 0
    from engine_state import ConfigError, load_config
    try:
        config = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    return _cmd_verify(config) if args.command == "verify" else _cmd_refresh(config)


if __name__ == "__main__":
    sys.exit(main())
