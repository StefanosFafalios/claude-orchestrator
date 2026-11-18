# OpenRouter Engine, Capability Map and Supercritical Tier — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OpenRouter as a third, budget-paced engine, replace per-engine model tiers with one model-centric capability map, choose reviewers by capability, and add a `supercritical` tier whose top model only reasons, plans and reviews.

**Architecture:** `scripts/model_map.py` owns the capability map (canonical models with per-engine names and scores; ranked model lists per role × route). `route_engine.py` becomes N-engine: for a role/route it finds, per usable engine, the highest-ranked model that engine can serve, then splits by headroom or picks the top-ranked. OpenRouter runs through the Codex CLI via `scripts/openrouter_run.py`, which supervises spend and pauses on soft-cap overruns; its budget math and caches live in `scripts/openrouter_budget.py`, and `usage_sources.py` turns the cached spend into a normal usage observation.

**Tech Stack:** Python 3.8+ stdlib (urllib, subprocess, signal, http.server in tests), Bash, plain JavaScript workflow, pytest, Node harness.

**Spec:** `docs/superpowers/specs/2026-11-18-openrouter-engine-design.md`

## Global Constraints

- Everything in `docs/AI_CONTEXT.md` applies: stdlib only, Python 3.8-compatible, `from __future__ import annotations`, module docstrings, hooks offline and < 5 s, wrappers exit 42 only for usage/budget limits, secrets through `engine_state.redact`, tests never call real `claude`/`codex`/network.
- Commits happen only if the owner has asked for them (AI_CONTEXT). The "Commit" steps below are checkpoints: run the suite, and commit only with owner approval. No AI attribution lines.
- Routes, in order: `trivial`, `standard`, `complex`, `critical`, `supercritical`.
- Engines, in order: `claude`, `codex`, `openrouter`.
- `engines.openrouter` defaults to `false`; OpenRouter is usable only when that flag is true **and** the env var named by `openrouter.key_env` (default `OPENROUTER_API_KEY`) is non-empty.
- Caps from a yearly allowance `Y` (USD, rounded to cents): `monthly_usd = Y/12`, `weekly_usd = 2·Y/52`, `daily_usd = 3·Y/365`.
- OpenRouter API base: `https://openrouter.ai/api/v1` (override `OPENROUTER_API_BASE` for tests). Key endpoint `GET /key`, catalog `GET /models`.
- Exit codes for `openrouter_run.py`: 0 success, 1 ordinary failure, 2 bad usage, 42 budget/usage limit (prints `OPENROUTER_USAGE_LIMIT`), 43 paused awaiting decision (prints `OPENROUTER_PAUSED <run-id>`).
- Auto mode means hook `permission_mode == "auto"` exactly; anything else, or missing, counts as off.
- Headless supercritical with no inherited answer: effort `medium` in auto mode; `requires_human_review` otherwise.
- The top model never implements: `supercritical` implementation uses the `critical` implementation row.
- Bump `.claude-plugin/plugin.json` `version` from `0.10.0` to `0.11.0`.

## Review Focus

1. **Project configs written by the old setup** (full copies of `defaults` and `codex.tiers`): routing must keep working and say once that those keys are ignored — never crash or exit 2. Test in Task 1.
2. **OpenRouter key unset but `engines.openrouter: true`** (new terminal without `source ~/.bashrc`): OpenRouter must be treated as disabled, not limited, and routing must fall through to Claude/Codex. Test in Task 2.
3. **Spend cache missing or stale** (first run, laptop offline): OpenRouter routes as healthy at 100 % only when no snapshot exists; a failed `/key` call never changes a run's exit code. Tests in Tasks 6 and 7.
4. **Resume after a pause while still over the cap:** the resumed run must not be paused again for the same window it was approved past. Test in Task 7.
5. **Reviewer equals implementer across engines** (Claude `sonnet` implements, OpenRouter `anthropic/claude-sonnet-…` is first in the review list): safeguard must skip it because it is the same canonical model. Test in Task 3.

---

### Task 1: Model-centric capability map

**Files:**
- Create: `scripts/model_map.py`
- Modify: `templates/orchestrator.config.json` (replace `defaults`, `codex.tiers`, `codex.default_model` with `models` + `routing`; keep `codex.max_files_per_fallback`)
- Modify: `scripts/engine_state.py` (`load_config`: validate map, record `legacy_keys`)
- Test: `tests/test_model_map.py`

**Interfaces:**
- Produces (in `model_map.py`):
  - `ROLES = ("implementation", "review", "scout", "style_review", "architecture")`
  - `ROUTES = ("trivial", "standard", "complex", "critical", "supercritical")`
  - `LEGACY_KEYS = ("defaults", "codex.tiers", "codex.default_model")`
  - `legacy_keys(project_config: dict) -> list[str]`
  - `validate(config: dict) -> None` — raises `engine_state.ConfigError` for: a routing row naming an unknown model; a row without `models` list or `effort`; effort `"ask"` outside `review`/`architecture` × `supercritical`; missing role or route row (except `implementation.supercritical`, which must be absent).
  - `row(config: dict, role: str, route: str) -> dict` → `{"models": [ids], "effort": str}`; `("implementation", "supercritical")` returns the `critical` row.
  - `score(config: dict, model_id: str) -> float`
  - `canonical(config: dict, engine: str, name: str) -> str | None` — reverse lookup of an engine-specific name (also accepts a canonical id).
  - `engine_name(config: dict, model_id: str, engine: str) -> str` — the engine's name for the model, or `""` when the model isn't offered there or isn't available (codex: present in `usage_sources.codex_models()` when that returns a set; openrouter: present in `usage_sources.openrouter_models()` when that returns a set — added in Task 6, until then treat as `None`; claude: always available).
- Config shape (template):

```json
"models": {
  "haiku":  {"score": 40, "engines": {"claude": "haiku"}},
  "sonnet": {"score": 70, "engines": {"claude": "sonnet"}},
  "opus":   {"score": 85, "engines": {"claude": "opus"}},
  "fable":  {"score": 95, "engines": {"claude": "claude-fable-5-2"}},
  "gpt-5.6-luna":  {"score": 45, "engines": {"codex": "gpt-5.6-luna"}},
  "gpt-6-luna":    {"score": 55, "engines": {"codex": "gpt-6-luna"}},
  "gpt-5.6-terra": {"score": 65, "engines": {"codex": "gpt-5.6-terra"}},
  "gpt-6.1-terra": {"score": 72, "engines": {"codex": "gpt-6.1-terra"}},
  "gpt-6.1-sol":   {"score": 82, "engines": {"codex": "gpt-6.1-sol"}},
  "gpt-6-astra":   {"score": 88, "engines": {"codex": "gpt-6-astra"}}
},
"routing": {
  "implementation": {
    "trivial":  {"models": ["haiku", "gpt-6-luna", "gpt-5.6-luna"], "effort": "low"},
    "standard": {"models": ["sonnet", "gpt-6.1-terra", "gpt-5.6-terra"], "effort": "medium"},
    "complex":  {"models": ["sonnet", "gpt-6.1-sol"], "effort": "high"},
    "critical": {"models": ["opus"], "effort": "high"}
  },
  "review": {
    "trivial":  {"models": ["haiku", "gpt-6-luna"], "effort": "low"},
    "standard": {"models": ["sonnet", "gpt-6.1-terra", "gpt-5.6-terra"], "effort": "medium"},
    "complex":  {"models": ["gpt-6.1-sol", "sonnet", "gpt-6-luna"], "effort": "high"},
    "critical": {"models": ["opus", "gpt-6-astra", "gpt-6-luna"], "effort": "high"},
    "supercritical": {"models": ["fable", "gpt-6-astra"], "effort": "ask"}
  },
  "architecture": {"<trivial..critical>": {"models": ["opus", "gpt-6-astra"], "effort": "high"},
                   "supercritical": {"models": ["fable", "gpt-6-astra"], "effort": "ask"}},
  "scout":        {"<all five routes>": {"models": ["haiku", "gpt-6-luna"], "effort": "low"}},
  "style_review": {"<all five routes>": {"models": ["haiku"], "effort": "low"}}
}
```

(`<…>` means: write one identical row per listed route.) Scores are placeholders until Task 11 replaces them with evidence.

- [ ] **Step 1: Write the failing tests** in `tests/test_model_map.py`:

```python
def test_supercritical_implementation_uses_critical_row(config):
    assert row(config, "implementation", "supercritical") == row(config, "implementation", "critical")

def test_canonical_reverse_lookup(config):
    assert canonical(config, "claude", "sonnet") == "sonnet"
    assert canonical(config, "codex", "gpt-6-luna") == "gpt-6-luna"
    assert canonical(config, "codex", "nope") is None

def test_codex_name_respects_models_cache(config, usage_files):
    write_models(usage_files, "gpt-6-luna")          # helper as in test_route_engine.py
    assert engine_name(config, "gpt-6-luna", "codex") == "gpt-6-luna"
    assert engine_name(config, "gpt-6.1-sol", "codex") == ""
    assert engine_name(config, "sonnet", "codex") == ""

def test_unknown_model_in_routing_is_config_error(write_config):
    write_config({"routing": {"review": {"trivial": {"models": ["ghost"], "effort": "low"}}}})
    with pytest.raises(ConfigError, match="ghost"):
        load_config()

def test_ask_effort_only_for_supercritical_review_or_architecture(write_config):
    write_config({"routing": {"implementation": {"trivial": {"models": ["haiku"], "effort": "ask"}}}})
    with pytest.raises(ConfigError, match="ask"):
        load_config()

def test_legacy_project_keys_are_reported_not_fatal(write_config):
    write_config({"defaults": {"scout_model": "haiku"}, "codex": {"tiers": {}, "max_files_per_fallback": 3}})
    config = load_config()
    assert config["legacy_keys"] == ["defaults", "codex.tiers"]
    assert config["codex"]["max_files_per_fallback"] == 3
```

- [ ] **Step 2: Run** `python3 -m pytest tests/test_model_map.py -q` — expect FAIL (`ModuleNotFoundError: model_map`).
- [ ] **Step 3: Implement** `model_map.py` and the template. In `engine_state.load_config`, after merging: drop `defaults`, `codex.tiers`, `codex.default_model` from the merged dict, set `config["legacy_keys"] = legacy_keys(project_raw)`, call `model_map.validate(config)`. `model_map` imports `ConfigError` lazily from `engine_state` to avoid a cycle (or move `ConfigError` into `model_map` and re-export it from `engine_state` — pick one, keep `engine_state.ConfigError` importable).
- [ ] **Step 4: Run** `python3 -m pytest tests/test_model_map.py -q` — expect PASS. The rest of the suite will fail until Task 3 (router still reads `defaults`); that is expected only between Tasks 1 and 3.
- [ ] **Step 5: Commit** (if approved): `Add model-centric capability map`.

---

### Task 2: Three engines in state and config

**Files:**
- Modify: `scripts/engine_state.py` (`ENGINES`, `engine_enabled`, state defaults, `set-effort` command)
- Modify: `templates/orchestrator.config.json` (`engines.openrouter`, `fallback`, `openrouter` block)
- Test: `tests/test_engine_state.py`

**Interfaces:**
- Produces:
  - `ENGINES = ("claude", "codex", "openrouter")`
  - `engine_enabled(config: dict, engine: str) -> bool` — `config["engines"].get(engine, engine != "openrouter")`, and for openrouter also `os.environ.get(config["openrouter"]["key_env"])` non-empty.
  - State gains top-level `"supercritical_efforts": {task_id: effort}`; `load_state`/`save_state` preserve it; `reset` clears it.
  - CLI `engine_state.py set-effort --task-id ID --effort {low,medium,high,xhigh,max}`; task id validated with `^[A-Za-z0-9._-]{1,64}$` (exit 2 otherwise).
- Template additions:

```json
"engines": {"claude": true, "codex": true, "openrouter": false},
"fallback": {"claude_to_codex": true, "codex_to_claude": true, "require_human_review_when_both_limited": true},
"openrouter": {"key_env": "OPENROUTER_API_KEY",
               "budget": {"yearly_usd": 0, "monthly_usd": 0, "weekly_usd": 0, "daily_usd": 0}}
```

Unlisted `<src>_to_<dst>` fallback keys default to true (as `fallback_allowed` already does).

- [ ] **Step 1: Write failing tests:** `test_openrouter_disabled_without_key` (flag true, env unset → `engine_enabled(...) is False`), `test_openrouter_enabled_with_key` (monkeypatch env → True), `test_state_has_three_engines` (`load_state()` keys ⊇ ENGINES), `test_set_effort_records_and_validates` (`main(["set-effort","--task-id","t1","--effort","high"])` → state `supercritical_efforts == {"t1": "high"}`; task id `"../x"` → `SystemExit` code 2 or return 2).
- [ ] **Step 2: Run** `python3 -m pytest tests/test_engine_state.py -q` — expect FAIL.
- [ ] **Step 3: Implement.** `sync_usage` loops `ENGINES` and skips engines without an observation (already does).
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** (if approved): `Track OpenRouter as a third engine in state`.

---

### Task 3: N-engine router with capability-based review

**Files:**
- Modify: `scripts/route_engine.py` (rewrite of selection logic; output shape kept)
- Modify: `scripts/classify_task.py` (drop `requires_codex_review` from output; it never returns `supercritical`)
- Test: `tests/test_route_engine.py` (rewrite where meaning changed), `tests/test_classify_task.py`

**Interfaces:**
- Consumes: `model_map.row/score/canonical/engine_name`, `engine_state.engine_enabled`, `ENGINES`.
- Produces:
  - `route(role, task_route, config, state, task_key=None, implementation_model=None, exclude=(), parent_task_id=None, permission_mode=None, headless=False) -> dict` returning `{"engine", "model", "effort", "canonical_model", "reason", "requires_human_review", "scope"}`. `implementation_model` is an engine-specific or canonical name, or `"auto"` (derive from `route("implementation", …, task_key)`). `parent_task_id/permission_mode/headless` are consumed in Task 4.
  - CLI flags: `--role`, `--task-route` (5 routes), `--task-id`, `--implementation-model`, `--exclude-engine` (repeatable), `--parent-task-id`, `--permission-mode`, `--headless`. Remove `--requires-codex-review` and `--implementation-engine`.
- Selection algorithm (shared helper `_select(engines, model_ids, task_key, split, exclude)`):
  1. `home` = among **enabled** engines (limited or not, excluded or not), the one serving the model earliest in `model_ids` (ties → `ENGINES` order). This is the engine the work belongs to when everything is healthy.
  2. For each engine in `ENGINES` order that is enabled, not `limited`, and not in `exclude`: candidate = first id in `model_ids` with non-empty `engine_name`. If `home` is constrained, limited or excluded, drop candidates `E ≠ home` where `fallback_allowed(home, E)` is false.
  3. Healthy candidates exist → if `split` and `task_key` and `balance.mode == "adaptive"` and route in `balance.routes` and ≥ 2 healthy: pick by `_bucket(task_key)` over healthy engines weighted by `headroom`; equal-zero headroom → equal weights. Else the healthy candidate earliest in `model_ids`.
  4. Only constrained candidates → the one with most `remaining_pct`, `scope = "single_atomic_task"`, `requires_human_review` per config (existing `_constrained` rule).
  5. None → `engine: "none"`.
  - `split=True` only for implementation.
- Role rules:
  - implementation: `_select(row("implementation", route).models, split=True)`.
  - review: impl id = `canonical(implementation_model)`; eligible = row models `≠ impl id` with `score ≥ score(impl id)`; if none, eligible = row models `≠ impl id` and set `requires_human_review` with reason `no reviewer at least as capable as the implementer`. Then `_select(eligible, split=False)`.
  - scout / architecture / style_review: `_select(row(...).models, split=False)`; style_review returns `none` (with `requires_human_review=False`) when `review.split` is false, route is `trivial`, or the chosen engine is not healthy.

- [ ] **Step 1: Write failing tests** (replace obsolete ones; keep `state()` helper but give it an `openrouter` entry defaulting to healthy/100):

```python
@pytest.mark.parametrize("task_route, model, effort", [
    ("trivial", "haiku", "low"), ("standard", "sonnet", "medium"),
    ("complex", "sonnet", "high"), ("critical", "opus", "high"), ("supercritical", "opus", "high")])
def test_healthy_implementation_tiers(config, task_route, model, effort): ...  # engine "claude"

def test_reviewer_never_same_model(config):
    r = route("review", "standard", config, state(), implementation_model="sonnet")
    assert r["canonical_model"] == "gpt-6.1-terra"

def test_reviewer_never_weaker_than_implementer(config):
    # complex row [gpt-6.1-sol 82, sonnet 70, gpt-6-luna 55]: nothing ≥ 82 except the implementer itself
    r = route("review", "complex", config, state(), implementation_model="gpt-6.1-sol")
    assert r["canonical_model"] == "sonnet" and r["requires_human_review"] is True

def test_same_model_across_engines_is_skipped(config, monkeypatch):
    config["models"]["sonnet"]["engines"]["openrouter"] = "anthropic/claude-sonnet-x"
    config["engines"]["openrouter"] = True; monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    r = route("review", "standard", config, state(), implementation_model="anthropic/claude-sonnet-x")
    assert r["canonical_model"] != "sonnet"

def test_three_way_split_uses_every_healthy_engine(config, monkeypatch): ...
    # openrouter enabled, standard row gets an openrouter-only model; 200 task keys → each engine chosen ≥ 1

def test_exclude_engine_moves_to_next(config):
    assert route("implementation", "standard", config, state(), exclude=("claude",))["engine"] == "codex"

def test_critical_never_implemented_off_claude(config):
    assert route("implementation", "critical", config, state(claude="limited", claude_pct=0))["engine"] == "none"

def test_openrouter_enabled_without_key_is_ignored(config): ...  # flag true, no env → never chosen
```

Keep (adapted) existing tests for: claude limited → codex; both constrained → more remaining + human review; fallback `claude_to_codex: false`; style review skipped on trivial; codex model availability via cache.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_route_engine.py tests/test_classify_task.py -q` — expect FAIL.
- [ ] **Step 3: Implement** the router and classifier changes. Delete `codex_tier`, `_claude`, `_claude_tier`, `_codex`, `CODEX_EFFORT`, `DEFAULT_BALANCED` (keep `("standard", "complex")` as the `balance.routes` fallback).
- [ ] **Step 4: Run** `python3 -m pytest -q` — router and classifier tests PASS. `test_hooks.py`/`test_workflow.py` may still fail until Tasks 5 and 8; note which.
- [ ] **Step 5: Commit** (if approved): `Route across N engines and pick reviewers by capability`.

---

### Task 4: Supercritical effort resolution

**Files:**
- Modify: `scripts/route_engine.py`
- Test: `tests/test_route_engine.py`

**Interfaces:**
- Produces: `supercritical_effort(state: dict, task_key: str | None, parent_task_id: str | None, permission_mode: str | None, headless: bool) -> tuple[str, bool]` → `(effort, requires_human_review)`:
  1. `state["supercritical_efforts"][task_key]` exists → `(it, False)`
  2. `permission_mode == "auto"` and parent recorded → `(parent's, False)`
  3. `headless` → `("medium", False)` if auto else `("", True)`
  4. otherwise `("ask", False)`
- Applied in `route()` only when the row's effort is `"ask"` (review/architecture × supercritical). Callers pass the **top-most** supercritical task id as `--parent-task-id`, so no chain walking is needed.

- [ ] **Step 1: Write failing tests:** `test_supercritical_asks_interactively` (effort `"ask"`), `test_recorded_effort_wins`, `test_auto_mode_inherits_parent`, `test_manual_mode_does_not_inherit` (parent recorded, mode `"default"` → `"ask"`), `test_headless_auto_defaults_medium`, `test_headless_manual_needs_human` (`requires_human_review is True`), `test_supercritical_architect_is_top_model` (canonical `fable`).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** (if approved): `Ask for the top model's effort on supercritical tasks`.

---

### Task 5: Prompt hook, dispatch guard and status for three engines and five tiers

**Files:**
- Modify: `hooks/scripts/inject_orchestration_context.py`
- Modify: `hooks/scripts/validate_agent_dispatch.py`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `route(...)` from Task 3/4, `engine_enabled`, `config["legacy_keys"]`.
- Behavior:
  - Engine-state line lists `openrouter=<status>(<pct>%)` only when `engine_enabled(config, "openrouter")`; otherwise unchanged text.
  - `legacy_keys` non-empty → one line: `Legacy config keys ignored: <keys>; models now come from "models"/"routing" (see /cost-aware-orchestrator:setup).`
  - Rubric line appends: `supercritical = needs frontier reasoning beyond known solutions (top model reasons, plans and reviews; never implements).` Rows are produced for all five tiers from the floor up; the keyword floor never yields `supercritical`.
  - Row labels: engine `claude` → agent name; `codex` → `Codex`; `openrouter` → `OpenRouter`. Implementation on codex/openrouter is marked `[Codex]`/`[OpenRouter]` with the existing "delegate, do not edit" line generalized.
  - Supercritical row shows `architect=architect/<model>/ASK` and `review=…/ASK` when effort is `ask`, plus one line: `Supercritical task id <task_key>: before the first top-model step ask the user which effort (low|medium|high|xhigh|max; cost rises steeply), then run engine_state.py set-effort --task-id <task_key> --effort <e>. Route its sub-tasks with route_engine.py --task-id <sub> --parent-task-id <task_key> --permission-mode <permission_mode>.`
  - `permission_mode` comes from the hook payload and is passed to `route(...)`.
  - OpenRouter run line (only when any row uses it): `OpenRouter: write_handoff.py, then OPENROUTER_MODEL=<model> OPENROUTER_EFFORT=<effort> OPENROUTER_SANDBOX=workspace-write openrouter_run.py <handoff>; exit 43 = paused over budget: tell the user and ask continue (--resume <run>) or stop (--stop <run>).`
  - Guard: `codex-reviewer` is denied when the engine named in its prompt is limited — `openrouter` if the prompt contains `openrouter_run.py`, else `codex`.

- [ ] **Step 1: Write failing tests:** `test_openrouter_hidden_when_disabled`, `test_openrouter_shown_when_enabled` (config flag + env key in `run_hook` env), `test_supercritical_row_and_effort_line` (prompt routed with mode `"auto"` contains `--permission-mode auto` and `ASK`), `test_legacy_keys_notice`, `test_guard_denies_openrouter_review_when_openrouter_limited`. Extend `run_hook` to forward `OPENROUTER_API_KEY`, `ORCHESTRATOR_OPENROUTER_USAGE_FILE`, `ORCHESTRATOR_OPENROUTER_MODELS_FILE` when set.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_hooks.py -q` — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS, and confirm hook runtime stays well under 5 s (`timeout=5` in `run_hook` already enforces it).
- [ ] **Step 5: Commit** (if approved): `Show OpenRouter and supercritical routes in the prompt hook`.

---

### Task 6: OpenRouter budget, caches and usage observation

**Files:**
- Create: `scripts/openrouter_budget.py`
- Modify: `scripts/usage_sources.py` (`openrouter_models`, `openrouter_usage`, `observe(config=None)`)
- Modify: `scripts/engine_state.py` (`sync_usage` passes config to `observe`)
- Modify: `tests/conftest.py` (autouse isolation of the two new cache paths)
- Test: `tests/test_openrouter_budget.py`, `tests/test_usage_sync.py`

**Interfaces:**
- Produces (in `openrouter_budget.py`):
  - `caps(yearly_usd: float) -> dict` → `{"yearly_usd", "monthly_usd", "weekly_usd", "daily_usd"}`
  - `api_base() -> str`, `usage_file() -> Path` (env `ORCHESTRATOR_OPENROUTER_USAGE_FILE`, default `~/.cache/cost-aware-orchestrator/openrouter-key.json`), `models_file() -> Path` (env `ORCHESTRATOR_OPENROUTER_MODELS_FILE`, default `…/openrouter-models.json`). Both are account-level, shared by all projects.
  - `fetch_key(key: str, timeout: float = 10) -> dict` (the `data` object of `GET /key`; raises `OSError`/`ValueError` on failure)
  - `fetch_models(timeout: float = 30) -> list[dict]`
  - `write_snapshot(data: dict, now: float) -> None` (atomic temp+rename; stores `usage_daily`, `usage_weekly`, `usage_monthly`, `limit`, `limit_remaining`, `limit_reset`, `observed_at`; never the key)
  - `read_snapshot() -> dict | None`
  - `windows(snapshot: dict, budget: dict, now: float) -> list[tuple[float, float | None]]` — `(used_pct, resets_at)` for day (next 00:00 UTC), week (next Monday 00:00 UTC), month (next 1st 00:00 UTC) against `daily/weekly/monthly_usd`; plus pace: `allowance = monthly_usd × min(1, elapsed_fraction_of_month + 1/days_in_month)`, `used = min(99, 100 × usage_monthly / allowance)`, `resets_at=None`. Spend from a snapshot observed in an earlier window counts as 0 for that window. Caps of 0 are skipped.
  - `over_soft_cap(snapshot, budget) -> str | None` → `"daily"`/`"weekly"` when spend ≥ cap.
  - `cap_spent(snapshot, budget, now) -> bool` → any of day/week/month used ≥ 100, or `limit_remaining` ≤ 0.
  - CLI: `caps --yearly-usd N` (prints JSON), `verify` (fetches `/key` with the env key; prints `{"key_valid", "limit", "limit_reset", "monthly_cap", "matches", "guardrail_budgets"}`; exit 1 when the key is missing/invalid; writes snapshot), `refresh` (fetches `/key` and `/models`; writes both caches).
- `usage_sources.openrouter_models() -> set[str] | None` (ids from the models cache; `None` when absent).
- `usage_sources.openrouter_usage(config: dict, now: float | None = None) -> dict | None` → `observation(windows(...), snapshot["observed_at"], "openrouter-key-cache", now)`; `None` when no snapshot or OpenRouter not enabled.

- [ ] **Step 1: Write failing tests:**

```python
def test_caps_from_yearly():
    assert caps(240) == {"yearly_usd": 240, "monthly_usd": 20.0, "weekly_usd": 9.23, "daily_usd": 1.97}

def test_pace_ahead_is_near_limit_not_limited():
    # mid-month, monthly 20, spent 15 → pace used capped at 99 → remaining 1
    obs = observation(windows(snap(usage_monthly=15), budget(), now=MID_MONTH), MID_MONTH, "t", MID_MONTH)
    assert 0 < obs["remaining_pct"] < 25

def test_spent_daily_cap_is_limited():
    assert cap_spent(snap(usage_daily=2.0), budget(), MID_MONTH) is True

def test_previous_day_spend_does_not_count_today(): ...
def test_no_snapshot_means_no_observation(config): assert openrouter_usage(config) is None
def test_snapshot_never_contains_key(tmp_path): ...  # write_snapshot({..., "label": "sk-or-v1-…"}) → file lacks "sk-or"
```

Use fixed epoch constants (`MID_MONTH = 2026-11-15T12:00Z`), never the clock.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_openrouter_budget.py tests/test_usage_sync.py -q` — FAIL.
- [ ] **Step 3: Implement.** `model_map.engine_name` switches its openrouter check to `usage_sources.openrouter_models()`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** (if approved): `Pace OpenRouter spend against the yearly allowance`.

---

### Task 7: OpenRouter runner with pause-and-ask supervision

**Files:**
- Create: `scripts/openrouter_run.py`
- Test: `tests/test_openrouter_run.py`

**Interfaces:**
- Consumes: `openrouter_budget.fetch_key/write_snapshot/read_snapshot/over_soft_cap/cap_spent`, `engine_state.load_config/project_dir/redact`.
- Produces: `openrouter_run.py <task-file> | --resume RUN_ID | --stop RUN_ID` with the Global Constraints exit codes.
  - Env: `OPENROUTER_MODEL` (required), `OPENROUTER_EFFORT`, `OPENROUTER_SANDBOX` (`read-only` default | `workspace-write`), `CODEX_BIN` (default `codex`), `OPENROUTER_POLL_SECONDS` (default 30).
  - Run dir: `.claude/orchestrator/openrouter-runs/<run-id>/` with `out`, `err`, `exit`, `pid`, `paused.json`. Run id `YYYYmmddTHHMMSS-<6 hex>`; `--resume/--stop` validate it with `^[0-9T]{15}-[0-9a-f]{6}$`.
  - Command: `sh -c '"$0" "$@"; echo $? > <run>/exit'` wrapping `codex exec --sandbox <S> --model <M> -c model_provider="openrouter" -c model_providers.openrouter.name="openrouter" -c model_providers.openrouter.base_url="<api_base()>" -c model_providers.openrouter.env_key="<key_env>" [-c model_reasoning_effort="<E>"] <prompt>`, started with `start_new_session=True`, stdin `/dev/null`.
- Behavior:
  1. Key env unset → exit 1 with `OPENROUTER_API_KEY is not set` (name from config). Snapshot `cap_spent` → exit 42 without starting.
  2. Every poll: `fetch_key` → `write_snapshot`; network errors ignored. `over_soft_cap` returns a window not in `approved_windows` → `os.killpg(pgid, SIGSTOP)`, write `paused.json` `{run_id, pid, window, spend, cap, task_file, approved_windows}`, print `OPENROUTER_PAUSED <run-id>`, exit 43.
  3. `--resume`: add the paused window to `approved_windows`, `SIGCONT`, keep supervising (poll `os.kill(pid, 0)` until gone), then read `exit`.
  4. `--stop`: `SIGCONT` then `SIGTERM` the group (a stopped process ignores TERM until continued), delete `paused.json`, exit 1 with `stopped by user`.
  5. On completion: one final `fetch_key` (errors ignored). Output matches `openrouter_key_limit|openrouter_credits|openrouter_guardrail_budget|openrouter_workspace_budget|Payment Required|\b402\b` → print `OPENROUTER_USAGE_LIMIT`, exit 42. Matches `openrouter_in_flight_budget` → sleep `OPENROUTER_POLL_SECONDS`, rerun once; still failing → exit 1. Otherwise pass through the exit code; stdout = codex stdout; stderr redacted.

- [ ] **Step 1: Write failing tests** with a fake `codex` (shell script: optional `sleep`, then prints/exit as chosen by env) and a fake `/key` server (`http.server` in a thread on `127.0.0.1:0`, JSON body mutable by the test; `OPENROUTER_API_BASE=http://127.0.0.1:<port>`), `OPENROUTER_POLL_SECONDS=0.2`:
  - `test_success_passes_output` (exit 0, stdout from codex, snapshot file written)
  - `test_missing_key_is_ordinary_error` (exit 1)
  - `test_spent_cap_exits_42_before_running` (fake codex never invoked: its marker file absent)
  - `test_402_key_limit_exits_42`
  - `test_auth_error_is_exit_1` (`401 Unauthorized`)
  - `test_over_daily_cap_pauses_with_43` (codex sleeps 5 s; server reports `usage_daily` over cap → exit 43 within 2 s, `paused.json` exists, process state `T` in `/proc/<pid>/stat`)
  - `test_resume_does_not_repause_same_window` (after 43, `--resume` with server still over → exit 0 when codex finishes)
  - `test_stop_terminates_group` (after 43, `--stop` → exit 1, pid gone)
  - `test_key_endpoint_down_does_not_change_exit` (server closed → exit 0)
  - `test_key_never_in_args` (fake codex records `$@`; key value absent)
- [ ] **Step 2: Run** `python3 -m pytest tests/test_openrouter_run.py -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — PASS; `ruff check .` clean.
- [ ] **Step 5: Commit** (if approved): `Run OpenRouter models through Codex with spend supervision`.

---

### Task 8: Workflow over N engines and five tiers

**Files:**
- Modify: `workflows/smart-task.js`
- Modify: `tests/workflow_harness.mjs` (nothing beyond passing `args.permission_mode` if needed)
- Test: `tests/test_workflow.py`

**Interfaces:**
- Consumes: router CLI (Task 3/4), `openrouter_run.py` (Task 7).
- Changes:
  - `ROUTES` gains `supercritical`; `reroute` never downgrades `critical` or `supercritical`.
  - Route commands pass `--headless --permission-mode <ARGS.permission_mode || 'default'>` and, when present, `--parent-task-id <ARGS.parent_task_id>` (validated `^[A-Za-z0-9._-]{1,64}$`). Review routes with `--implementation-model <impl.model>` after implementation (or `auto` before).
  - Delete `claudeTier`, `codexTier`, `CODEX_EFFORT`, `fallbackAllowed`, `requiresCodexReview`. Fallback after a limit: mark the engine, then re-route implementation with `--exclude-engine` for every tried engine; `engine: none` → stop with human review.
  - `runOpenRouter(tier, handoffPath)` mirrors `runCodex` using `OPENROUTER_MODEL/EFFORT/SANDBOX` and `openrouter_run.py`; exit 42 → limited; exit 43 → stop the workflow with `requires_human_review: true` and summary `OpenRouter paused over its <window> budget: run openrouter_run.py --resume <run> or --stop <run>.`
  - `plan()` runs for `complex`, `critical`, `supercritical`; when the architecture engine is codex/openrouter, run its wrapper read-only with the plan prompt and append the output tail as the plan. Architecture effort `""` with `requires_human_review` (headless manual supercritical) → return the human-review result before any top-model call.
  - Review: engine `claude` → `verifier` agent with the routed model/effort; `codex`/`openrouter` → `codex-reviewer` agent whose prompt names the wrapper (`codex_with_fallback.sh` or `openrouter_run.py`) and model.
  - Handoff `--target-engine` accepts `openrouter` (check `write_handoff.py` choices; extend if restricted) and caps files with `codex.max_files_per_fallback` for both non-Claude targets.
- [ ] **Step 1: Write failing tests:** add a fake OpenRouter key server fixture (reuse from Task 7 via a small helper in `conftest.py`) and env for `OPENROUTER_API_KEY`.
  - `test_fallback_claude_to_openrouter_when_codex_limited` (claude limited → openrouter run via fake codex, `fallback_events[0]["to"] == "openrouter"` when codex also limited)
  - `test_openrouter_pause_stops_for_human` (exit 43 path → `requires_human_review` and `--resume` in summary)
  - Supercritical scenarios pass `args.classification = {"route": "supercritical", "reason": "test"}` (the keyword classifier never yields it).
  - `test_supercritical_headless_auto_uses_medium` (`args.permission_mode="auto"` → plan agent effort `medium`, model `claude-fable-5-2`, implementer model `opus`)
  - `test_supercritical_headless_manual_stops_before_top_model` (no `plan` call; `requires_human_review`)
  - `test_review_uses_capability_not_engine` (update `test_standard_task_on_claude`: review engine is `codex` because `gpt-6.1-terra` is the first eligible reviewer)
- [ ] **Step 2: Run** `python3 -m pytest tests/test_workflow.py -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python3 -m pytest -q` — whole suite PASS.
- [ ] **Step 5: Commit** (if approved): `Run smart-task across three engines and the supercritical tier`.

---

### Task 9: Agents, setup walkthrough and status command

**Files:**
- Modify: `agents/codex-reviewer.md` (engine-neutral relay: runs the wrapper its prompt names; marks the matching engine limited on 42; on 43 reports the pause and does not retry)
- Modify: `agents/architect.md` (supercritical: reason, map tasks with a tier per task, never implement)
- Modify: `commands/setup.md` (new optional step "OpenRouter")
- Modify: `commands/engine-status.md` (show OpenRouter spend vs caps via `openrouter_budget.py verify` when enabled, and any pending pauses in `.claude/orchestrator/openrouter-runs/*/paused.json`)
- Modify: `skills/cost-aware-orchestration/SKILL.md`, `skills/cost-aware-orchestration/references/routing-policy.md` (three engines, capability map, review rule, supercritical, pause semantics)
- Test: `tests/test_hooks.py` already covers guard; add `test_setup_mentions_key_location` only if setup text is asserted elsewhere — otherwise no test (prose).

**Setup step content (exact order):**
1. Ask whether to enable OpenRouter; skip on no.
2. Account: open https://openrouter.ai, sign in, Settings → Credits → add credits.
3. Ask the yearly allowance in USD; run `openrouter_budget.py caps --yearly-usd <N>` and show monthly/weekly/daily.
4. Settings → Keys → Create Key: name `cost-aware-orchestrator`; credit limit = monthly cap; reset = monthly.
5. Guardrails: check Settings for Guardrails/budgets; if present, add a daily budget (daily cap) and a weekly budget (weekly cap) to that key; if absent, say the plugin enforces daily/weekly by pausing runs.
6. Key storage: add `export OPENROUTER_API_KEY="sk-or-v1-…"` to `~/.bashrc` (or `~/.zshrc`), then `source` it or open a new terminal. Never in the project, never pasted into chat.
7. Run `openrouter_budget.py verify`; on `matches: false` show the mismatch and repeat step 4. Then `openrouter_budget.py refresh`.
8. Write `engines.openrouter: true` and `openrouter.budget` (all four values) into the project config.

- [ ] **Step 1:** Edit the files above. **Step 2:** `python3 -m pytest -q && ruff check .` — PASS. **Step 3: Commit** (if approved): `Guide OpenRouter setup and document the new routing`.

---

### Task 10: Maintainer skill `review-models`

**Files:**
- Create: `.claude/skills/review-models/SKILL.md`
- Create: `.claude/skills/review-models/catalog.py`
- Create: `.claude/skills/review-models/decision-log.md` (header only)
- Test: `tests/test_review_models_catalog.py`

**Interfaces:**
- `catalog.py`: `filter_models(models: list[dict], min_context: int = 64000) -> list[dict]` keeps entries with `"tools"` in `supported_parameters`, `context_length ≥ min_context`, id not ending `:free`/`:batch`, not starting `~`, prompt price > 0, no `expiration_date` in the past (when the field exists); returns `[{id, name, context_length, prompt_usd_per_mtok, completion_usd_per_mtok, blended_usd_per_mtok}]` with blended = (3·prompt + completion)/4, sorted by blended. CLI `catalog.py [--input FILE] [--min-context N]` reads `/models` (or a file) and prints JSON.
- `SKILL.md` frontmatter: `name: review-models`, `description: Use when reviewing which OpenRouter/Claude/Codex models the plugin should route to, or when new models are released.` Body = the five steps from the spec (Catalog → Evidence → Fit → Proposal → Apply) with:
  - Evidence rules: coding/agentic benchmarks only (SWE-bench Verified, Terminal-Bench, Artificial Analysis coding/intelligence index or equivalents), each score with source URL and date; OpenRouter rankings are not quality evidence; composite `score` 0–100 written into `models.<id>.score`.
  - Fit rules: per role × route a minimum score bar; list every model clearing the bar ordered cheapest-first for implementation/scout/style, strongest-first for review/architecture; supercritical rows hold only the top models; same canonical model across engines = one `models` entry with several `engines` names.
  - Proposal: a table (role, route, current, proposed, score, blended price, reason); stop for owner approval.
  - Apply: edit `templates/orchestrator.config.json`, bump plugin version, smoke-test each new OpenRouter slug with `OPENROUTER_MODEL=<slug> OPENROUTER_SANDBOX=read-only openrouter_run.py <tiny task that lists one file>` (requires the owner's key), drop models that fail tool calls, append a dated entry to `decision-log.md` (what changed, evidence links, spend of the smoke tests).
- [ ] **Step 1: Write failing test** `test_filter_models` with a 6-entry fixture covering each exclusion and the blended sort. **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** (if approved): `Add maintainer skill for reviewing routed models`.

---

### Task 11: Seed the default map (owner key required)

**Files:**
- Modify: `templates/orchestrator.config.json` (`models` scores, OpenRouter names and OpenRouter-only models; `routing` rows)
- Modify: `.claude/skills/review-models/decision-log.md`
- Test: existing suite

- [ ] **Step 1:** Run the `review-models` skill steps 1–4 (network: `/models` and benchmark sources). Present the proposal table to the owner and wait for approval.
- [ ] **Step 2:** Ask the owner to add `export OPENROUTER_API_KEY="…"` to `~/.bashrc` and `source` it; verify with `test -n "$OPENROUTER_API_KEY" && echo set` (never print the value). Run `openrouter_budget.py verify`.
- [ ] **Step 3:** Apply step 5 (smoke tests, template update, decision log). Also write top-level `"map_meta": {"reviewed": "<YYYY-MM-DD>", "log": ".claude/skills/review-models/decision-log.md"}` into the template (spec: review date and decision-log reference); `model_map.validate` ignores it.
- [ ] **Step 4:** `python3 -m pytest -q` — PASS (tests that pin placeholder scores/model ids must use their own config fixtures, not the template, so they survive the seed; fix any that don't).
- [ ] **Step 5: Commit** (if approved): `Seed the capability map from benchmark evidence`.

---

### Task 12: Docs and version

**Files:**
- Modify: `docs/AI_CONTEXT.md` (repo map: `model_map.py`, `openrouter_budget.py`, `openrouter_run.py`, `.claude/skills/review-models/`; constraints: exit 43, OpenRouter caches isolated in tests via the two env vars)
- Modify: `skills/cost-aware-orchestration/references/coding-standards.md` only if a shared rule changed (exit-code rule now includes 43)
- Modify: `README.md` (OpenRouter section, supercritical tier, capability map)
- Modify: `.claude-plugin/plugin.json` (`0.11.0`)
- [ ] **Step 1:** Edit. **Step 2:** `python3 -m pytest -q && ruff check . && shellcheck scripts/*.sh` — PASS. Smoke test: `claude --plugin-dir .` in a scratch project; submit one standard and one supercritical prompt; confirm hook rows. **Step 3: Commit** (if approved): `Document OpenRouter routing and bump to 0.11.0`.
