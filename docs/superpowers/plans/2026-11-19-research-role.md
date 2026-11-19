# Web Research Role Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `research` routing role that searches the web only when a task needs outside knowledge and hands a cited report to planning and implementation.

**Architecture:** New optional `routing.research` rows (subscriptions first, Perplexity Sonar on OpenRouter last). Claude runs a `researcher` agent with web tools, Codex runs with `--search`, OpenRouter runs one direct chat-completions call via `scripts/research_run.py`. The decider (or session) flags `needs_research`; the workflow adds a Research phase after Scout.

**Tech Stack:** Python 3 stdlib (3.8-compatible), Bash wrapper, plain JS workflow, pytest + Node harness.

**Spec:** `docs/superpowers/specs/2026-11-19-research-role-design.md`

## Global Constraints

- New models: `sonar` → `perplexity/sonar`, `sonar-reasoning-pro` → `perplexity/sonar-reasoning-pro`, `sonar-deep-research` → `perplexity/sonar-deep-research` (OpenRouter only).
- Rows: trivial unused; standard `["haiku", "gpt-6.1-terra", "sonar"]` low; complex `["sonnet", "gpt-6.2-sol", "sonar-reasoning-pro"]` medium; critical and supercritical `["opus", "gpt-6.2-astra", "sonar-deep-research"]` high. Never `fable`.
- Configs without `routing.research` load and never research; nothing research-related appears in the hook output.
- Queries: generic questions only; always passed through `engine_state.redact` and capped at 2000 characters; no code, paths or project names.
- `research_run.py` exit codes: 0 report on stdout; 42 usage limit (HTTP 402 / key limit) only; 1 any other failure; 2 bad usage. The key goes only in the Authorization header. It refuses (exit 1, message) when OpenRouter is disabled, constrained, or a budget cap is spent (cached snapshot, no `/key` call).
- A failed or skipped research step never blocks the task.
- Python stdlib only, 3.8-compatible; workflow JS has no fs/process/Date.now/Math.random; offline tests only (fake OpenRouter fixture). Local commits, one per task, no AI attribution lines, `Refs #3` (last task `Closes #3`).

## Review Focus

- A query containing a secret-looking token → redacted before it reaches the fake server body.
- OpenRouter answers 402 → exit 42 and the workflow marks OpenRouter limited and moves on without research.
- Decider omits `needs_research` or sends a non-boolean → treated as false.
- Trivial task flagged `needs_research` → no Research phase.
- Research step fails on every engine → the task still runs Plan and Implement.

---

### Task 1: Research rows and the OpenRouter research runner

**Files:**
- Modify: `templates/orchestrator.config.json` (models + `routing.research`), `tests/capability_map.json` (same rows)
- Modify: `scripts/model_map.py` (`ROLES` gains `research`; `UNUSED` gains `("research", "trivial")`; a config without `routing.research` loads, like `docs`/`decider`)
- Create: `scripts/research_run.py`
- Test: `tests/test_research_run.py` (new; reuse `fake_openrouter` from `tests/conftest.py`), `tests/test_model_map.py`

**Interfaces:**
- Produces: `research_run.py --query-file F --model <openrouter slug> [--effort E]` → Markdown report on stdout ending with a `Sources:` list (from the response's citations/annotations when present) and a `Cost:` line when the response reports it; `route_engine.py --role research`.

- [ ] **Step 1: Write failing tests** — validate accepts the template and a config without `routing.research`; `route("research", "standard", …)` with all engines healthy picks claude/haiku; with claude and codex limited picks openrouter/`perplexity/sonar`; `route("research", "trivial", …)` → engine none. Runner: success prints report + sources; a `sk-…`-looking token in the query never reaches the fake server body; query over 2000 chars is truncated; key absent from argv; 402 → exit 42; 500/timeout/invalid JSON → exit 1; OpenRouter disabled or cap spent → exit 1 with no HTTP call.
- [ ] **Step 2: Run** the new tests → FAIL.
- [ ] **Step 3: Implement.** Mirror `scripts/decide_route.py` for the HTTP call, budget check and redaction; system prompt asks for concise findings with sources and dates and forbids speculation.
- [ ] **Step 4: Run** `python3 -m pytest -q && ruff check .` → PASS.
- [ ] **Step 5: Commit** `Add research routing rows and the OpenRouter research runner`.

### Task 2: Engines and detection

**Files:**
- Create: `agents/researcher.md` (tools: WebSearch, WebFetch, Read; model sonnet default; returns the same report shape)
- Modify: `scripts/codex_with_fallback.sh` (`CODEX_SEARCH=1` adds `--search`), `scripts/decide_route.py` (asks for and returns `needs_research` boolean; missing/non-boolean → false), `hooks/scripts/inject_orchestration_context.py` (only when `routing.research` exists: one short line telling the session to set `needs_research` when outside knowledge is needed, and a `research=` entry in non-trivial rows)
- Test: `tests/test_wrappers.py`, `tests/test_decide_confidence.py`, `tests/test_hooks.py`

- [ ] **Step 1: Write failing tests** — fake codex receives `--search` only with `CODEX_SEARCH=1`; decider returns `needs_research` true/false and false for missing or `"yes"`; hook without `routing.research` is byte-identical in research terms (no `research`/`needs_research` text) and default caps hold; with it, rows show `research=` and the one instruction line appears once.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Keep `shellcheck` clean.
- [ ] **Step 4: Run** `python3 -m pytest -q && ruff check .` → PASS.
- [ ] **Step 5: Commit** `Add the researcher agent, Codex search switch and research detection`.

### Task 3: Workflow Research phase, docs and release

**Files:**
- Modify: `workflows/smart-task.js` (Research phase), `commands/smart-task.md`, `skills/cost-aware-orchestration/SKILL.md`, `skills/cost-aware-orchestration/references/routing-policy.md`, `README.md`, `docs/AI_CONTEXT.md`, `.claude/skills/review-models/SKILL.md`, `.claude-plugin/plugin.json` (`0.13.0`)
- Test: `tests/test_workflow.py`

**Interfaces:**
- Consumes: `classification.needs_research`, optional `classification.research_query`; architect PLAN output may include `research_query` (string).

- [ ] **Step 1: Write failing tests** — no flag → no `research` call; flagged standard task with Claude healthy → `researcher` agent call before `plan`, report text appears in the plan and implement prompts; Claude and Codex limited → `research_run.py` command with quoted `--model perplexity/sonar`; exit 42 → OpenRouter marked limited, task continues; all engines fail → task continues; trivial flagged → no research; architect returns `research_query` when no research ran → one research call after plan, report added to the implement prompt only; Codex path sets `CODEX_SEARCH=1` and read-only sandbox.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_workflow.py` → FAIL.
- [ ] **Step 3: Implement** a `researchPass(query)` modeled on `scoutPass`: route role `research`; Claude → `researcher` agent; Codex → wrapper with `CODEX_SEARCH=1`, read-only; OpenRouter → `research_run.py --query-file … --model …`; 42 → mark limited and re-route excluding; any other failure → log and continue. The query is `classification.research_query` or the task text, written to a file via the existing heredoc helper. Docs: one or two sentences each; review-models Fit rule (research rows from search/research benchmarks, subscriptions first, never fable). Bump `plugin.json` to `0.13.0`.
- [ ] **Step 4: Run** `python3 -m pytest -q && ruff check .` → PASS.
- [ ] **Step 5: Commit** `Run web research when a task needs outside knowledge` with body `Closes #3`.
