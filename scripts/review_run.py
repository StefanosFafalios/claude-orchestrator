#!/usr/bin/env python3
"""Code review on OpenRouter in one model call: the request file (task, diff, test output) in, findings out.

Usage: OPENROUTER_MODEL=<slug> [OPENROUTER_EFFORT=<e>] review_run.py <request-file>
An agentic run resends its whole growing context on every tool call (millions of tokens for one
review); here the reviewer sees only the request, once, so it must hold the diff with context.
Exit 0 prints the findings and a ``Cost:`` line (when reported); 42 usage limit (HTTP 402 or a key
limit) and nothing else; 2 bad usage or config; 1 any other failure, including a request over
MAX_REQUEST_CHARS (split the diff) and OpenRouter disabled, constrained or over budget. The request
is redacted; the key travels only in the Authorization header.
"""

from __future__ import annotations

import os
import sys

from engine_state import EFFORTS, ConfigError, load_config, load_synced_state, redact
from research_run import DEEP_TIMEOUT, Failure, chat, cost_line

MAX_REQUEST_CHARS = 400_000  # about 100k tokens: one call stays cents, not dollars
MAX_TOKENS = 16000
INSTRUCTIONS = ("You are an independent code reviewer. Review only the diff and context in the request; you "
                "cannot open files, so when a finding depends on unseen code, say what to check. Return "
                "severity-ranked findings (blocker, high, medium, low), each with file:line, a category "
                "(correctness, efficiency, security, or what the request asks for), a concrete failure "
                "scenario and a suggested fix. Skip pure taste. No findings: say so.")


def review(request: str, model: str, effort: str | None, config: dict, state: dict) -> str:
    text = redact(request, values_only=True)
    if len(text) > MAX_REQUEST_CHARS:
        raise Failure(f"request is {len(text)} characters (limit {MAX_REQUEST_CHARS}): split the diff into smaller reviews")
    # MAX_TOKENS bounds reasoning plus findings: an unbounded high-effort call ran past the timeout and
    # billed with no answer.
    body = {"model": model, "max_tokens": MAX_TOKENS,
            "messages": [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": text}]}
    if effort:
        body["reasoning"] = {"effort": effort}
    answer = chat(body, config, state, DEEP_TIMEOUT)  # a long diff at high effort takes minutes
    choice = answer["choices"][0]
    findings = (choice["message"].get("content") or "").strip()
    if not findings:
        raise Failure("OpenRouter returned an empty review (reasoning may have used up max_tokens)")
    if choice.get("finish_reason") == "length":  # never pass a cut-off list as the whole review
        findings += ("\n\n- high review_run: TRUNCATED at max_tokens; later findings are missing. "
                     "Re-run at lower effort or split the diff.")
    return findings + cost_line(answer)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    model, effort = os.environ.get("OPENROUTER_MODEL", ""), os.environ.get("OPENROUTER_EFFORT") or None
    if len(argv) != 1 or not os.path.isfile(argv[0]) or not model or (effort and effort not in EFFORTS):
        print("usage: OPENROUTER_MODEL=<slug> [OPENROUTER_EFFORT=<effort>] review_run.py <request-file>", file=sys.stderr)
        return 2
    try:
        with open(argv[0], encoding="utf-8") as handle:
            request = handle.read()
        if not request.strip():
            raise Failure("the request is empty", 2)
        config = load_config()
        print(review(request, model, effort, config, load_synced_state(config)))
    except Failure as exc:
        print(f"review: {redact(str(exc))}", file=sys.stderr)
        return exc.code
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - network, timeout, bad JSON or file errors: one failure code, never a fallback
        print(f"review failed: {redact(str(exc))[:300]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
