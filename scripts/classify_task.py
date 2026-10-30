#!/usr/bin/env python3
"""Deterministic, keyword-based task classifier. Never calls an LLM.

Reads the task from ``--task-file`` or stdin and prints a JSON classification.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

TIERS = {
    "trivial": ("haiku", "low"),
    "standard": ("sonnet", "medium"),
    "complex": ("sonnet", "high"),
    "critical": ("opus", "high"),
}

# Always critical: production, irreversible, credentials, data/schema, money, security flaws.
CRITICAL = {
    "production": r"\bprod(uction)?\b|\blive (system|environment|traffic)\b",
    "incident": r"\b(incident|outage|sev[0-3]|hotfix)\b",
    "credentials": r"\b(credentials?|secrets?|api[ -]?keys?|private keys?|certificates?)\b|\brotat(e|ion|ing)\b",
    "migration": r"\bmigrat(e|es|ion|ions|ing)\b|\bschema change\b|\balter table\b",
    "payment": r"\b(payments?|billing|invoic(e|es|ing)|checkout|refunds?|stripe)\b",
    "security": r"\b(vulnerab\w*|cve-\d+|xss|csrf|sql injection|injection|exploit\w*|encrypt\w*|decrypt\w*)\b",
    "irreversible": r"\b(irreversibl\w*|drop (the )?(table|database|db|column)|truncate|"
    r"delete (the )?(all|data|records|users|branch|db|database)|purge|wipe|"
    r"force[- ]push|rewrite history|rm -rf|reset --hard|push -f)\b|(?<!\w)--force\b",
    "deployment": r"\b(deploy(ment|ing|s)?|release to|rollout|roll out)\b",
    "blast_radius": r"\b(entire|whole) (codebase|repo(sitory)?|system|app(lication)?)\b|"
    r"\b(every|all) (services|modules|endpoints|callers)\b",
}

# Sensitive domains: critical when changed, complex when only investigated.
SENSITIVE = {
    "auth": r"\b(auth\w*|login|logout|oauth|sso|sessions?|passwords?|permissions?|rbac|acl|jwt|"
    r"(access|refresh|bearer|auth|api|session|csrf) tokens?)\b",
}

COMPLEX = {
    "debugging": r"\b(debug\w*|root cause|investigat\w*|diagnos\w*|troubleshoot\w*|why (does|is|do))\b",
    "uncertain": r"\b(intermittent\w*|flak(y|iness)|sporadic\w*|race condition|deadlock|memory leak|heisenbug|"
    r"non-?deterministic)\b",
    "architecture": r"\b(architect\w*|redesign\w*|re-?architect\w*|design (a|the) system)\b",
    "multi_component": r"\b(across (services|modules|the codebase|components)|multi-?(file|service|module)|"
    r"cross-?(service|module|cutting)|distributed|concurren\w*|performance|scal(ing|ability))\b",
}

STANDARD = {
    "feature": r"\b(add|implement\w*|build|create|make|support|feature|endpoint|integrat\w*|handl(e|ing))\b",
    "tests": r"\b(tests?|unit tests?|test coverage|pytest|jest|spec)\b",
    "docs": r"\b(document\w*|docs|docstrings?|changelog)\b",
    "refactor": r"\b(refactor\w*|extract|clean ?up|simplif\w*|update|change|modify|fix\w*|bug)\b",
}

TRIVIAL = {
    "typo": r"\b(typos?|spelling|misspel\w*|wording|grammar)\b",
    "formatting": r"\b(format\w*|whitespace|indent\w*|lint fix|prettier|black)\b",
    "rename": r"\brenam(e|ing)\b",
    "lookup": r"\b(what (does|is)|where is|find where|look ?up|explain|show me|list)\b",
    "tiny_edit": r"\b(one[- ]line|single[- ]line|bump (the )?version|comment)\b",
}

INVESTIGATION_ONLY = re.compile(COMPLEX["debugging"] + "|" + COMPLEX["uncertain"], re.IGNORECASE)
FOLLOW_UP = re.compile(
    r"^\s*(yes|yep|ok(ay)?|sure|go ahead|proceed|do it|lgtm|sounds good|commit|push)\b",
    re.IGNORECASE,
)
FILE_REF = re.compile(r"\b[\w./-]+\.(py|js|jsx|ts|tsx|go|rs|java|kt|rb|php|cs|c|cc|cpp|h|hpp|sh|sql|ya?ml|toml|json|md)\b")
CRITICAL_PATHS = re.compile(r"(^|/)(migrations?|alembic|terraform|\.github/workflows|k8s|helm)(/|\b)|\.sql\b|Dockerfile")
LONG_TASK_WORDS = 60
SHORT_FOLLOW_UP_WORDS = 8


def _hits(text: str, rules: dict) -> list[str]:
    return [name for name, pattern in rules.items() if re.search(pattern, text, re.IGNORECASE)]


def is_follow_up(text: str) -> bool:
    return bool(FOLLOW_UP.match(text) and len(text.split()) <= SHORT_FOLLOW_UP_WORDS
                and not _hits(text, CRITICAL) and not CRITICAL_PATHS.search(text))


def effective_text(task: str, replying_to: str = "") -> str:
    if is_follow_up(task) and replying_to.strip():
        paragraphs = re.split(r"\n\s*\n", replying_to.strip())
        # The offer may precede a table or list summarizing unrelated work.
        paragraph = next((part for part in reversed(paragraphs) if "?" in part), replying_to.strip()[-600:])
        return f"{paragraph} {task}"
    return task


def classify(task: str, replying_to: str = "") -> dict:
    initial = _classify(task)
    if initial["route"] == "critical" or not is_follow_up(task):
        return initial
    if not replying_to.strip():
        return _result("standard", "follow-up without context; defaulting to standard", False)
    result = _classify(effective_text(task, replying_to), follow_up=True)
    result["reason"] = "follow-up to: " + result["reason"]
    return result


def _classify(task: str, follow_up: bool = False) -> dict:
    text = " ".join(task.split())
    if not text:
        return _result("trivial", "empty task: nothing to orchestrate", False)

    word_count = len(text.split())
    files = {m.group(0) for m in FILE_REF.finditer(text)}
    critical = _hits(text, CRITICAL)
    if CRITICAL_PATHS.search(text):
        critical.append("critical_path")
    sensitive = _hits(text, SENSITIVE)
    complex_hits = _hits(text, COMPLEX)
    standard = _hits(text, STANDARD)
    trivial = _hits(text, TRIVIAL)
    if follow_up and re.search(r"\b(commit|push)\b", text, re.IGNORECASE):
        trivial.append("git_action")

    if critical:
        return _result("critical", "critical signals: " + ", ".join(critical), True)
    if sensitive and not INVESTIGATION_ONLY.search(text):
        return _result("critical", "changes a sensitive area: " + ", ".join(sensitive), True)
    if complex_hits or len(files) >= 3:
        reasons = complex_hits + ([f"{len(files)} files referenced"] if len(files) >= 3 else [])
        return _result("complex", "complex signals: " + ", ".join(reasons), bool(sensitive))
    if trivial and word_count <= LONG_TASK_WORDS and len(files) <= 1:
        # A trivial keyword only wins when no broader work is requested alongside it.
        broader = [hit for hit in standard if hit in ("feature", "tests")]
        if not broader:
            return _result("trivial", "trivial signals: " + ", ".join(trivial), False)
    if standard:
        return _result("standard", "standard signals: " + ", ".join(standard), False)
    return _result("standard", "no specific signals; defaulting to standard", False)


def _result(route: str, reason: str, requires_codex_review: bool) -> dict:
    model, effort = TIERS[route]
    return {
        "route": route,
        "recommended_model": model,
        "recommended_effort": effort,
        "reason": reason,
        "requires_codex_review": requires_codex_review,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task-file", help="file containing the task description (default: stdin)")
    parser.add_argument("--replying-to-file", help="file containing the previous assistant message")
    args = parser.parse_args(argv)
    if args.task_file:
        with open(args.task_file, encoding="utf-8") as handle:
            task = handle.read()
    else:
        task = sys.stdin.read()
    replying_to = ""
    if args.replying_to_file:
        with open(args.replying_to_file, encoding="utf-8") as handle:
            replying_to = handle.read()
    print(json.dumps(classify(task, replying_to), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
