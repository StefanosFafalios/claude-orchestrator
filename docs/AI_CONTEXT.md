# Shared AI Context

This is the canonical source of project knowledge and conventions for all agents
(Claude Code, Codex and others). Read it before making changes. Keep shared guidance
here; keep `CLAUDE.md` and `AGENTS.md` focused on their respective tools. Update this
document when repository behavior or workflows change. Source code determines
implemented behavior; flag stale documentation rather than treating it as a requirement.

## Purpose

`cost-aware-orchestrator` is a reusable Claude Code plugin. It routes work to the
cheapest sufficient model/effort across three engines (Claude, Codex and the optional
OpenRouter), falls back between them on usage limits through resumable handoffs, and
picks reviewers by capability: the cheapest model in the tier's review row that is at
least as capable as the implementer (within 3 points when the row has none stronger) and
never the exact same model. Top models (Fable, GPT-6.2-astra) appear only in supercritical rows;
Fable runs only on OpenRouter (the Claude subscription silently swaps it for another model, so
the dispatch guard denies Agent calls naming a model with no `claude` engine name). It must work in any project: no repository-specific paths, commands,
model names or credentials are hard-coded. Per-project settings live in the consuming
project's `.claude/orchestrator.config.json`.

## Repository map

| Path | Role |
|---|---|
| `.claude-plugin/` | `plugin.json` manifest and `marketplace.json` only |
| `skills/cost-aware-orchestration/` | auto-activating policy skill and its references |
| `skills/request-feature/` | skill that files generic GitHub feature requests for missing plugin behavior |
| `commands/` | `/cost-aware-orchestrator:*` slash commands |
| `agents/` | tiered subagents (scout, implementer, architect, verifier, style-reviewer, codex-reviewer, doc-updater) |
| `workflows/smart-task.js` | Workflow-tool script behind `/smart-task` |
| `hooks/` | `UserPromptSubmit` policy injection, `PreToolUse` dispatch guardrail |
| `scripts/` | deterministic Python routing/state/handoff logic and Bash CLI wrappers |
| `.claude/skills/review-models/` | maintainer-only skill for reviewing routed models (not shipped) |
| `templates/` | default config and handoff template |
| `tests/` | offline pytest suite, including a Node harness for the workflow |

`scripts/engine_state.py` owns config loading, state I/O and secret redaction; the
other scripts and hooks import from it rather than re-implementing them.
`scripts/model_map.py` resolves the model-centric capability map (`models`, `routing`),
including optional task-type sub-rows. In the adaptive split OpenRouter only takes a row model
that no healthy Claude or Codex engine serves (a later one in the row if needed).
`scripts/openrouter_budget.py` derives spend caps and owns the OpenRouter key/model caches;
`scripts/openrouter_run.py` runs OpenRouter models through Codex and supervises spend.
`scripts/decide_route.py` is the cheap tier decider: one direct OpenRouter call on the
`routing.decider` row, clamped to the keyword floor; exit 3 (never 42) hands classification
back to the session model and marks nothing. It also reports `confidence`, `alternative` and
`ask` (confidence below `decider.min_confidence`, default 0.9): the session then asks the user.
`scripts/research_run.py` is the OpenRouter research runner (one chat call, redacted and capped
generic query, report with sources; exit 42 on a usage limit). `workflows/smart-task.js`
`researchPass` runs it, the `researcher` agent or Codex with `CODEX_SEARCH=1` when the decider
flags `needs_research`; OpenRouter never receives the task text. The `Cost:` line a research report
prints is not written to the cached spend snapshot, so the cap check can lag until the next `/key`
refresh. The `sonar` models' `score` values are placeholders, not coding scores: keep them out of
review and implementation rows.
`scripts/usage_sources.py` reads observed quota from local files only (Claude status line
input, Codex session logs); `engine_state.sync_usage` applies it. Tests must isolate both
sources (the autouse `usage_files` fixture) and never read the real `~/.claude` or `~/.codex`.
OpenRouter caches are isolated with `ORCHESTRATOR_OPENROUTER_USAGE_FILE` and
`ORCHESTRATOR_OPENROUTER_MODELS_FILE`; tests never touch the network.

## Platform constraints

- Reference plugin files only through `${CLAUDE_PLUGIN_ROOT}`; never absolute paths.
- Workflow scripts are plain JavaScript with no filesystem, process, `import()`,
  `Date.now()` or `Math.random()`. `smart-task.js` runs commands through a minimal
  Haiku `bash()` agent and receives `timestamp` and `plugin_root` via `args`.
- The prompt hook supplies per-tier routes and a keyword safety floor; the main model
  classifies using the rubric and full conversation. The floor checks only literal prompt
  text. The dispatch guard no longer checks architect tier; the rows guide architect use.
- Hooks must finish well under their 5 s timeout, never call `claude`, `codex` or the
  network, and degrade to a notice instead of failing the prompt.
- `additionalContext` must be nested in `hookSpecificOutput`. The dispatch guard emits
  only `deny`; compliant dispatches produce no output so user permission rules apply.
- Wrapper exit code 42 means usage limit and nothing else; 43 (OpenRouter runner only)
  means paused awaiting the user's decision and never triggers a fallback. Auth, network,
  permission and ordinary errors exit 1 and must never trigger a fallback.

## Coding standards

- Python 3.11+ is the target; code stays compatible with 3.8+ because hooks run on
  whatever `python3` the user has. Standard library only. Four-space indent,
  snake_case, module docstrings, `from __future__ import annotations`.
- Bash wrappers stay thin: `set -uo pipefail`, quote every expansion, `shellcheck`-clean.
- JavaScript only in `workflows/`; no dependencies, no TypeScript syntax.
- No new dependencies. Prefer deletion over addition, the standard library over custom
  code, and the smallest diff that solves the problem.
- Match surrounding code: naming, comment density, idiom. Comments explain *why*.
- Fail explicitly: invalid config exits 2 with a clear message; never swallow errors
  that could lose data.
- Validate anything interpolated into shell commands (task ids, paths) and quote it.
- Never persist or hand off secrets: route text through `engine_state.redact`.
- Every behavior change ships with an offline test. Tests never invoke the real
  `claude` or `codex`; use the fake CLIs and the workflow harness.

## Verification

```bash
python3 -m pytest -q      # offline suite (workflow tests need node)
ruff check .
shellcheck scripts/*.sh   # when installed
```

Smoke-test the plugin with `claude --plugin-dir /path/to/this/repo` in a scratch project.

## Commits and pull requests

These rules apply to every agent.

- Commit, push or open PRs only when the owner asks.
- One logical change per commit; imperative subject of 72 characters or fewer, a body
  explaining why when it is not obvious. Reference the GitHub issue when one exists
  (`Refs #N` for partial work, `Closes #N` when resolved).
- **No agent attribution**: commit messages and pull requests carry no
  `Co-Authored-By` trailer for an AI agent and no "Generated with" line.
- Run the verification commands before committing; do not commit failing tests.
- Never commit secrets, `.env` files, or runtime state (`.claude/engine-state.json`,
  `.claude/orchestrator/`).
- Never force-push, rewrite published history or bypass hooks (`--no-verify`) unless
  the owner explicitly asks.
- Bump `version` in `.claude-plugin/plugin.json` for user-visible plugin changes.

## Orchestrated-work standards

`skills/cost-aware-orchestration/references/coding-standards.md` is the subset of these
rules the plugin hands to every engine in consuming projects. Keep the two consistent.

## Instruction synchroniser

`scripts/sync_instructions.py` plans or applies a shared instruction file for each
scope. Root instructions default to `docs/AI_CONTEXT.md`; nested scopes use a local
`AI_CONTEXT.md`. Claude and Codex instruction files become pointers to that common
file. A private journal and temporary backups allow the next `--apply` to recover an
interrupted run; successful runs remove them. Run without `--apply` to review the
plan and see whether recovery is pending.


## Claude Code-specific behavior

This repository *is* a Claude Code plugin. To try local changes, run
`claude --plugin-dir .` from a scratch project rather than installing from the
marketplace.

## Codex-specific behavior

Use Codex tools for repository work. Claude Code skills, hooks, agents and workflows
in this repository are product code you edit; Codex does not need Claude Code's skill
discovery to work on them. When a handoff from `.claude/orchestrator/handoffs/` is
your input, do only its "Remaining work" and nothing listed under "Do not redo".
