# Task-Type Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Optional per-task-type overrides of routing rows, with the type detected by the decider or the session model.

**Architecture:** A routing row may hold `types.<type>` sub-rows. `model_map.row()` returns the sub-row for a known type, so everything after row lookup (effort precedence, reviewer strength, fallbacks) is unchanged. The decider adds `type` to its JSON; the hook shows type lines only when the config has type rows; the workflow passes `--task-type`.

**Tech Stack:** Python 3 stdlib (3.8-compatible), plain JS workflow, pytest + Node harness.

**Spec:** `docs/superpowers/specs/2026-11-19-task-type-routing-design.md`

## Global Constraints

- Task types: exactly `frontend`, `debug`, `refactor`, `tests`, `docs`. Anything else (missing, `general`, unknown) means no type and uses the tier row.
- Configs without `types` behave exactly as today; the template ships no type rows.
- Invalid config exits 2 with a clear `ConfigError` message.
- Default-mode hook output stays under the existing 2400/2100 test caps; nothing type-related is printed when no type rows exist.
- `decide_route.py`: exit 0 + JSON or exit 3, never 42; no new HTTP call.
- Python stdlib only, 3.8-compatible; workflow JS has no fs/process/Date.now/Math.random.
- Every behavior change has an offline test. Commits are local, one per task, no AI attribution lines, `Refs #1` in the body.

## Review Focus

- Decider returns `"Frontend"` or `" tests "` → normalized (strip, lower-case) before validation.
- A review type row holding only models weaker than the implementer → the existing strength rule still falls back to a stronger model or flags human review.
- `implementation` + `supercritical` with a type → resolves through the `critical` row, then its type sub-row.
- `docs` role on a config without `routing.docs` → type lookup happens on the `style_review` row.
- Type sub-row with `effort: "ask"` under a row where `ask` is not allowed → exit 2.

---

### Task 1: Type rows in the map and the router

**Files:**
- Modify: `scripts/model_map.py` (constants, `validate`, `row`)
- Modify: `scripts/route_engine.py` (`route`, CLI)
- Test: `tests/test_task_types.py` (new; reuse fixtures from `tests/test_route_engine.py` as `tests/test_model_effort.py` does)

**Interfaces:**
- Produces: `model_map.TASK_TYPES = ("frontend", "debug", "refactor", "tests", "docs")`; `model_map.task_type(value) -> str | None` (strip + lower; None unless in TASK_TYPES); `model_map.has_type_rows(config) -> bool`; `model_map.row(config, role, route, task_type=None) -> dict`; `route(..., task_type: str | None = None)`; CLI `route_engine.py --task-type <any string>` (normalized with `task_type`).

- [ ] **Step 1: Write failing tests** — validation: accepts a row with `types.frontend` `{models, effort}` and with optional `model_effort`; rejects an unknown type key, a non-dict `types`, an unknown model, a bad effort, `model_effort` naming a model outside the sub-row, and `ask` in a sub-row whose parent role × tier is not in `ASK_ALLOWED`. Routing: `route("implementation", "complex", cfg, st, task_type="frontend")` picks the sub-row's model; `task_type="general"` / `None` / `"weird"` picks the tier row; `"Frontend"` normalizes; supercritical implementation with a type uses the critical row's sub-row; `docs` without `routing.docs` uses `style_review`'s sub-row; a review type row with only weaker models still yields a reviewer at least as strong or `requires_human_review`; the review role's `"auto"` writer lookup uses the same type; CLI `--task-type frontend` prints the sub-row's model.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_task_types.py` → FAIL.
- [ ] **Step 3: Implement.** In `validate`, factor the existing per-row checks into a helper and apply it to each `types` value (same `where` prefix plus `.types.<type>`). `row()` resolves role/route remaps first, then returns `entry["types"][t]` when `t = task_type(task_type)` is present there, else the entry. `route()` passes `task_type` to `row()` and to its internal `route("implementation", …)` call.
- [ ] **Step 4: Run** `python3 -m pytest -q && ruff check .` → PASS.
- [ ] **Step 5: Commit** `Allow per-task-type overrides in routing rows`.

### Task 2: Detect the type and show it in the hook

**Files:**
- Modify: `scripts/decide_route.py` (INSTRUCTIONS, output)
- Modify: `hooks/scripts/inject_orchestration_context.py` (`steps_for`, `directive`)
- Test: `tests/test_decide_confidence.py`, `tests/test_hooks.py`

**Interfaces:**
- Consumes: `model_map.TASK_TYPES`, `task_type`, `has_type_rows`; `route(..., task_type=)`.
- Produces: decider JSON gains `"type": "<type>"` only when valid; `steps_for(tier, config, state, task_key, permission_mode="", task_type=None)`.

- [ ] **Step 1: Write failing tests** — decider: answer `{"type": "Frontend", …}` → output `type == "frontend"`; `"type": "cooking"` or missing → no `type` key; exit codes unchanged. Hook: default config → output contains no `frontend` and no `/<type>` text; config with `routing.implementation.complex.types.frontend` → output has one types line listing the five types and the `route=<tier>/<type>` rule, and a `complex/frontend:` line containing only entries that differ from the `complex:` row; default-mode caps still pass.
- [ ] **Step 2: Run** the two test files → FAIL.
- [ ] **Step 3: Implement.** Decider: add `"type": "<frontend|debug|refactor|tests|docs or general>"` to the requested JSON and copy `model_map.task_type(answer.get("type"))` into the result when not None. Hook: `steps_for` passes `task_type` to every `route` call. In `directive`, when `has_type_rows(config)`: append `Types (only if clearly one): frontend, debug, refactor, tests, docs: state route=<tier>/<type>, use its <tier>/<type> line.` and, for each built tier and each type present in any of that tier's rows, a `<tier>/<type>: …` line with only the `; `-separated entries that differ from the tier row (skip when none differ).
- [ ] **Step 4: Run** `python3 -m pytest -q && ruff check .` → PASS.
- [ ] **Step 5: Commit** `Detect task type in the decider and show type rows in the hook`.

### Task 3: Workflow, docs and release

**Files:**
- Modify: `workflows/smart-task.js` (Classify phase, `ROUTE_FLAGS`)
- Modify: `commands/smart-task.md`, `skills/cost-aware-orchestration/SKILL.md`, `skills/cost-aware-orchestration/references/routing-policy.md`, `README.md`, `docs/AI_CONTEXT.md`, `.claude/skills/review-models/SKILL.md`
- Modify: `.claude-plugin/plugin.json` (version `0.12.0`)
- Test: `tests/test_workflow.py`

**Interfaces:**
- Consumes: `route_engine.py --task-type`, decider `type`.
- Produces: workflow arg `classification.type` (optional).

- [ ] **Step 1: Write failing tests** — workflow: `classification: {route: "complex", type: "frontend", reason}` → every `route_engine.py` command contains `--task-type frontend`; no type → no `--task-type` flag; when the workflow runs the decider and it returns `type: "tests"`, routes carry `--task-type tests`; a type with shell metacharacters is quoted (`q()`), not interpolated raw.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_workflow.py` → FAIL.
- [ ] **Step 3: Implement.** Workflow: after classification, `const TYPE = String(classification.type || '')`; append ` --task-type ${q(TYPE)}` to `ROUTE_FLAGS` when non-empty (ROUTE_FLAGS is built before Classify today: move or extend it after). Docs, one or two sentences each: smart-task.md (pass `type` in `classification` when the hook asked for one); SKILL.md and routing-policy.md (type rows, `route=<tier>/<type>`, unknown → tier row, reviewer rule unchanged); README (one line in the routing section); AI_CONTEXT.md (`model_map.py` resolves type sub-rows); review-models SKILL.md Fit rule (type rows only from per-type benchmark evidence, cheapest-sufficient first, same reviewer coverage rule as tier rows; no evidence, no row). Bump `plugin.json` to `0.12.0`.
- [ ] **Step 4: Run** `python3 -m pytest -q && ruff check .` → PASS.
- [ ] **Step 5: Commit** `Pass task type through the workflow and document type rows` with body `Closes #1`.
