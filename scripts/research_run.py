#!/usr/bin/env python3
"""Web research on OpenRouter: one search-grounded model call, a Markdown report on stdout.

Usage: research_run.py --query-file F --model <openrouter slug> [--effort E]
Exit 0 prints the report, ending with a ``Sources:`` list (when the response cites any) and a
``Cost:`` line (when it reports one). Exit 42 means usage limit (HTTP 402 or a key limit) and
nothing else; 2 is bad usage or config; 1 is any other failure, including OpenRouter being
disabled, constrained or over budget (cached snapshot, no ``/key`` call). The query is a generic
question: it is redacted and capped, and the key travels only in the Authorization header.
A failed research step never blocks the task: the caller moves on without it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

import openrouter_budget as ob
from engine_state import EFFORTS, ConfigError, effective_status, engine_enabled, load_config, load_synced_state, redact

TIMEOUT = 120
MAX_QUERY_CHARS = 2000
INSTRUCTIONS = ("Answer the question with concise findings. Cite a source and its date for every claim. "
                "Do not speculate: say plainly when sources do not settle the question.")


class Failure(Exception):
    """Research cannot run or failed; ``code`` is the exit code."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def research(query: str, model: str, effort: str | None, config: dict, state: dict) -> str:
    if not engine_enabled(config, "openrouter"):
        raise Failure("OpenRouter is not set up")
    status = effective_status(state["openrouter"], config)
    if status != "healthy":
        raise Failure(f"OpenRouter is {status}")
    snapshot = ob.read_snapshot()  # cached: no /key call; cap_spent is window-aware
    if snapshot and ob.cap_spent(snapshot, config["openrouter"]["budget"], time.time()):
        raise Failure("OpenRouter budget is spent")
    key = os.environ.get(config["openrouter"]["key_env"])
    if not key:
        raise Failure("OpenRouter has no API key")
    body = {"model": model,
            "messages": [{"role": "system", "content": INSTRUCTIONS},
                         {"role": "user", "content": redact(query)[:MAX_QUERY_CHARS]}]}  # redact first: a cut token would slip past
    if effort:
        body["reasoning"] = {"effort": effort}
    request = urllib.request.Request(ob.api_base() + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = redact(exc.read().decode("utf-8", "replace"))[:200]
        limited = exc.code == 402 or (exc.code == 403 and "limit" in detail.lower())
        raise Failure(f"OpenRouter answered {exc.code}: {detail}", 42 if limited else 1) from exc
    message = answer["choices"][0]["message"]
    report = (message.get("content") or "").strip()
    if not report:
        raise Failure("OpenRouter returned an empty report")
    urls = list(answer.get("citations") or [])
    urls += [a["url_citation"]["url"] for a in message.get("annotations") or [] if a.get("type") == "url_citation"]
    urls = list(dict.fromkeys(u for u in urls if isinstance(u, str)))
    if urls:
        report += "\n\nSources:\n" + "\n".join(f"- {u}" for u in urls)
    cost = (answer.get("usage") or {}).get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        report += f"\n\nCost: ${cost:.4f}"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--query-file", required=True, help="file containing a generic research question")
    parser.add_argument("--model", required=True, help="OpenRouter model slug, e.g. perplexity/sonar")
    parser.add_argument("--effort", choices=EFFORTS)
    args = parser.parse_args(argv)
    try:
        with open(args.query_file, encoding="utf-8") as handle:
            query = handle.read().strip()
        if not query:
            raise Failure("the query is empty", 2)
        config = load_config()
        print(research(query, args.model, args.effort, config, load_synced_state(config)))
    except Failure as exc:
        print(f"research: {redact(str(exc))}", file=sys.stderr)
        return exc.code
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - network, timeout, bad JSON or file errors: one failure code, never a fallback
        print(f"research failed: {redact(str(exc))[:300]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
