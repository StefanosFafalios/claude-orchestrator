# OpenRouter as a third engine — design

Date: 2026-11-18. Status: draft for owner approval.

## Intent

The plugin routes each task to the cheapest sufficient model and falls back between the
Claude and Codex subscriptions on usage limits. The owner wants more total AI usage
without losing quality. OpenRouter adds a pay-per-token pool of models (including Claude
and OpenAI models) under a yearly USD allowance the user chooses. Cheap work goes to
inexpensive models, hard work to strong ones, and model choice comes from a map of each
model's capabilities rather than a fixed tier name.

## Goals

1. OpenRouter is a full third engine (`openrouter`) alongside `claude` and `codex` in state,
   routing, fallback, review, hooks, the `/smart-task` workflow and status commands.
2. When configured and within budget, OpenRouter is a **regular lane**: it takes a share of
   work all the time, not only when the subscriptions run out.
3. Spend is paced so the yearly allowance lasts the year.
4. Model choice per role and tier comes from a capability map in the plugin defaults,
   overridable per project.
5. `/cost-aware-orchestrator:setup` offers OpenRouter integration and walks the user through
   creating a key, storing it, and setting spending limits on the OpenRouter website.
6. A maintainer-only skill in this repo documents and runs the process for reviewing
   OpenRouter models and updating the capability map.
7. A `supercritical` tier uses the top model for reasoning, task mapping and review only;
   implementation stays on cheaper tiers.
8. Reviewers are chosen by capability per tier, never weaker than the implementer, never the
   same model that implemented.

## Non-goals

- OpenCode or any agent harness other than the Claude Code and Codex CLIs.
- Automatic, unattended model-map updates. Map changes always pass through owner approval.
- Managing OpenRouter credits/payment. The user tops up on the website.
- Programmatic key creation (management API keys). The user creates the key by hand.

## Anti-goals (failure even if it works)

- Spending more than the user's yearly allowance, or a runaway task draining a month's budget.
- The key appearing in any project file, state file, handoff, log or commit.
- A model reviewing its own output, or a reviewer weaker than the implementer.
- A plugin with no OpenRouter key behaving any differently from today (all current tests pass
  unchanged in meaning).
- Hooks calling the network or exceeding their timeout.

## Constraints

- All existing constraints in `docs/AI_CONTEXT.md` hold: stdlib Python 3.8-compatible, hooks
  offline and < 5 s, wrappers exit 42 only for usage/budget limits, secrets through
  `engine_state.redact`, offline tests with fake CLIs, no new dependencies.
- OpenRouter facts this design relies on (docs checked 2026-11-18):
  - An API key has one credit `limit` with one `limit_reset` (`daily` | `weekly` | `monthly` | none).
  - Guardrail budgets (`limit_usd` + `reset_interval`) can add more caps, but are documented as
    an org-admin feature; availability on personal accounts is unconfirmed.
  - `GET https://openrouter.ai/api/v1/key` returns `usage_daily`, `usage_weekly`,
    `usage_monthly`, `limit`, `limit_remaining`, `limit_reset`, `budgets`, `effective_budget`.
    Windows are UTC (week starts Monday).
  - Exhausted limits fail with HTTP 402 (`limit_source` `openrouter_key_limit` or
    `openrouter_credits`) or 403 (`openrouter_guardrail_budget` / `openrouter_workspace_budget`).
    `openrouter_in_flight_budget` is transient (retry after `Retry-After`), not a limit.
  - `GET /api/v1/models` is public and returns ids, pricing, context length and supported
    parameters.
  - Codex CLI works against OpenRouter with a `model_providers.openrouter` entry
    (`base_url = "https://openrouter.ai/api/v1"`) and accepts any OpenRouter slug. Claude Code
    through OpenRouter is only guaranteed with Anthropic first-party models.

## Approaches considered

- **A. Full third engine (chosen by the owner).** Generalize state and routing from two engines
  to N. OpenRouter's "remaining quota" is its budget pacing. Most consistent long term; largest
  change to `route_engine.py`.
- **B. Cheap lane in front of the router.** A pre-step sends mapped roles/tiers to OpenRouter
  while under daily pace; Claude/Codex logic untouched. Smaller, but OpenRouter would never be a
  reviewer and stays a special case.
- **C. OpenRouter models as extra Codex models.** Rejected: a Codex usage limit and an
  exhausted OpenRouter budget would share one state entry and block each other wrongly.

## Design

### Engine availability

`openrouter` is enabled when `engines.openrouter` is true in config (default **false**) and the
key environment variable (default `OPENROUTER_API_KEY`, name configurable as
`openrouter.key_env`) is set. Disabled means identical behavior to today.

### Budget and pacing → `remaining_pct`

Config (`openrouter.budget`): `yearly_usd`, and derived caps written by setup:
`monthly_usd = yearly/12`, `weekly_usd = 2 × yearly/52`, `daily_usd = 3 × yearly/365`
(multipliers are the owner-approved proposal; they allow bursts while the monthly cap keeps the
year within budget).

`remaining_pct` for OpenRouter is the minimum over:

- each window (day, week, month): `100 × (1 − spend / cap)`;
- **pace**: `100 × (1 − month_spend / (monthly_usd × elapsed_fraction_of_month))`, clamped to
  [1, 100], with a small floor on the elapsed fraction so the first hours of a month aren't 0.
  The pace term never goes below 1, so running ahead of pace makes OpenRouter `near_limit`
  (deprioritized), never `limited`; only a spent cap makes it `limited`.

So OpenRouter looks `healthy` while spending at or under an even pace, `near_limit` when ahead
of pace, and `limited` when any cap is spent. This makes the existing headroom-weighted split
spread the budget across the month instead of spending it as fast as daily caps allow.

### Usage observation

- The wrapper calls `GET /api/v1/key` after every run (success or failure) and writes a small
  cache file under the project's `.claude/orchestrator/` (spend per window, limit, limit_reset,
  observed_at). Network happens only in the wrapper, setup and status commands.
- The cache is written atomically (temp file + rename, as `save_state` does); last write wins
  under parallel runs, which is fine because each write is a full server-side snapshot. A failed
  `/key` call after a run is ignored (the run's own exit code stands; the cache stays stale).
- `usage_sources.openrouter_usage()` reads that cache (offline) and returns an observation in
  the same shape as the Claude/Codex observations; `sync_usage` applies it unchanged.
- Windows reset at UTC boundaries, so `windows_reset` is computable from `observed_at` alone.

### Wrapper: `scripts/openrouter_with_fallback.sh`

OpenRouter's Codex guide documents non-OpenAI slugs (e.g. `~anthropic/claude-sonnet-latest`)
working through Codex; the maintainer skill's smoke test verifies each mapped model does tool
calls through `codex exec`, and models that fail are not mapped. No custom agent runtime.
Same contract as `codex_with_fallback.sh` (exit 0 / 42 / 1 / 2, same env names prefixed
`OPENROUTER_`). Runs `codex exec` with per-run `-c` overrides for `model_provider=openrouter`
and the provider block, so the user's own `~/.codex/config.toml` is untouched. Exit 42 on
402/403 with a limit `limit_source`; the transient in-flight budget error is retried once after
its `Retry-After`, then exit 1. Auth errors exit 1. The key never appears in arguments (Codex
reads it from the environment).

**Mid-run overspend: pause and ask (owner decision).** While a run is active the wrapper polls
`/api/v1/key` (about every 30 s). If the daily or weekly cap is exceeded mid-run, it never
kills the run: it suspends the Codex process group (`SIGSTOP`), records a pending decision
(run id, spend vs cap, task) under `.claude/orchestrator/`, and exits with a new code **43**
("paused, awaiting decision"). The main session notifies the user (message plus push
notification) and asks continue or stop. `--resume <run>` sends `SIGCONT` and keeps
supervising; `--stop <run>` terminates it and the remaining work goes through the normal
handoff. A single HTTP request cannot be paused: the pause takes effect between requests (one
per agent step), so the step in flight at pause time completes and bills, and a long pause may
make Codex retry that step on resume. Stopping closes the stream; for providers that support
stream cancellation (OpenAI, Anthropic, DeepSeek, xAI, Together, Fireworks, …) that stops
billing immediately, for the rest the in-flight step still bills. Provider choice is not
constrained by cancellation support (owner decision); the map routes whatever the model review
finds best. The key's monthly limit stays the
hard server-side cap. Exit 42 keeps meaning "limit reached" only.

### Routing (N engines)

- `ENGINES = ("claude", "codex", "openrouter")`. `Engines` already computes per-engine status;
  extend loops and `fallback_allowed(src, dst)` (keys `<src>_to_<dst>`, all default true).
- **Implementation split:** among healthy engines that can take the route (enabled, model
  resolved, tier `implement` switch on), the task hash picks an engine with probability
  proportional to headroom. Two-engine behavior is unchanged when OpenRouter is disabled.
- **Constrained cases:** a constrained engine is skipped while any healthy candidate exists;
  if all candidates are constrained, use the one with the most remaining %, single atomic task,
  human review — the existing rule generalized.
- **Review (owner decision):** the reviewer is chosen by capability, not by engine or vendor
  (the same model can be reached through several engines, so "different engine" is not
  independence). Pick the best review model for the task's tier from the capability map, at
  least as capable as the implementer's model, on whichever engine can serve it (same quota and
  split rules as implementation). **Safeguard:** if that pick is the exact same model that
  implemented (same canonical model, any engine), use the next-best model of comparable
  capability. This replaces the current cross-engine review rule.
- **Scout, style review, architecture:** OpenRouter may serve them per the capability map, as a
  candidate in the same headroom split; architecture keeps preferring Claude while healthy.
- **Critical implementation** on OpenRouter follows the tier's `implement` switch, default false
  (same stance as Codex).
- **Re-tiering, test gate, handoffs** are engine-agnostic already; the "escalate to the other
  engine" step becomes "the next engine by the same split rules, excluding the one that failed".
- **Fallback order** after an engine hits a limit: headroom split among the remaining usable
  engines whose `<src>_to_<dst>` fallback is allowed.
- **Model identity:** the capability map is model-centric. Each entry is one canonical model with
  its name per engine (e.g. Claude engine `sonnet`, OpenRouter `anthropic/claude-sonnet-…`;
  Codex `gpt-6-luna`, OpenRouter `openai/gpt-6-luna`), its capability scores and prices. That is
  how "same model" and "at least as capable" are decided across engines, and why the map covers
  Claude and Codex models too, not only OpenRouter-exclusive ones.

### Capability map

Model-centric (see Routing → Model identity): a `models` catalog of canonical models, each with
per-engine names, capability scores (with source and date) and prices; and per role
(`implementation`, `review`, `scout`, `style_review`, `architecture`) × tier a ranked list of
canonical models with effort and the `implement` switch. Every listed model clears that tier's
bar, so all are sufficient: the router splits work by headroom across the usable engines that
can serve any listed model, and each engine uses its highest-ranked model it can serve. That
keeps prepaid subscription quota in use while OpenRouter spends at pace. Existing `defaults` / `codex.tiers` keys keep working as overrides so current
project configs don't break; the exact migration is left to the builder. Model resolution
skips slugs missing from a cached copy of `/api/v1/models` (written by setup/status, like
Codex's `models_cache.json`), else trusts the first entry. Each entry in the defaults carries a
comment-free sibling `openrouter.map_meta` with the review date and decision-log reference.

### New tier: `supercritical` (owner decision)

A fifth route above `critical`, for work that needs the best available reasoning rather than
more size or risk: problems beyond current state of the art, novel algorithms or proofs, root
causes that defeated a critical-tier attempt.

- **What runs on the top model (owner decision): reasoning, task mapping and review only —
  never implementation.** The architect role uses the top entry of the map's `supercritical`
  list (e.g. Fable 5.2 via Claude, or the strongest equivalent on Codex/OpenRouter). It works out the hard reasoning and produces a plan detailed enough for cheaper models
  to execute; each planned task is then classified and implemented on its own tier, at most
  `critical`. The `supercritical` tier has `implement: false` on every engine, so even the
  hard-reasoning core is implemented by the critical-tier implementer following the top
  model's plan. The top model then reviews the result.
- **Effort is the user's choice (owner decision).** The top model is too expensive to default to
  maximum effort. Before the top model first runs on a supercritical task, the main session asks
  the user which effort to use (the engine's levels, e.g. low/medium/high/xhigh/max, with a note
  that cost rises steeply), and that answer applies to the task's planning and review.
  - *Auto mode on:* ask once on the top-most supercritical task; its supercritical descendants
    in the same plan inherit the answer. A supercritical task under a non-supercritical parent
    is asked once and passes its answer down.
  - *Auto mode off:* ask on every supercritical task separately.
  - *Can't ask (headless run):* in auto mode, inherit an answer from a supercritical ancestor if
    there is one, otherwise use `medium`. With auto mode off, pause with `requires_human_review`
    (outside auto mode every supercritical task gets its own answer, so nothing is inherited).
  - Auto mode is read from the permission mode Claude Code passes to hooks (exact field and value
    verified during the build). If it can't be determined, treat auto mode as off.
- **Ordering:** linear, above `critical`. It inherits critical's safeguards (architect required,
  never downgraded, human review when engines are constrained). Review uses the top model (the
  same-model safeguard never triggers, since the top model never implements).
- **How work gets there:** the main model's classification against a rubric line ("needs
  frontier reasoning; beyond known solutions"), an explicit user request, or a re-tier after a
  `critical` attempt fails. No keyword floor: false positives are expensive and the floor only
  sees literal text.
- **Touches:** `ROUTES` in `route_engine.py`, `classify_task.py`, the prompt hook's rubric and
  rows, `smart-task.js`, the routing-policy reference, the map, and tests.

### Setup walkthrough (`commands/setup.md`)

Offered as an optional step. In text, step by step:

1. Create/sign in to an OpenRouter account, add credits (Settings → Credits).
2. Ask the user's yearly allowance in USD; show the derived monthly/weekly/daily caps.
3. Settings → Keys → Create key: name it, set **credit limit = monthly cap, reset monthly**.
4. Check whether guardrails are available on the account; if so, guide adding daily and weekly
   budgets to the key. If not, explain that the plugin enforces daily/weekly caps itself.
5. Store the key in the shell profile as `export OPENROUTER_API_KEY=...` (never in the project);
   restart the terminal.
6. Setup verifies by calling `/api/v1/key`: key valid, limit and reset match, budgets present.
   Writes `engines.openrouter: true` and the budget to the project config.

### Maintainer skill: `.claude/skills/review-models/`

Lives in this repo only (not under `skills/`, so it does not ship). `SKILL.md` documents and
runs the review process; a stdlib script fetches and filters the catalog.

1. **Catalog**: fetch `/api/v1/models`; keep models with tool calling, adequate context,
   not deprecated, not `:free`; record prices.
2. **Evidence**: collect coding/agentic benchmark scores (e.g. SWE-bench Verified,
   Terminal-Bench, Artificial Analysis indices) with source URL and date for each number.
   Usage rankings are not quality evidence.
3. **Fit**: per role/tier minimum quality bar; among models that clear it, pick the cheapest by
   blended price; for review, rank the best models per tier and keep a comparable runner-up for
   the same-model safeguard.
4. **Proposal**: current vs proposed table with prices, scores and reasons; owner approves.
5. **Apply**: update `templates/orchestrator.config.json`, bump plugin version, smoke-test each
   new slug with one tiny call using the maintainer's key, append a dated entry to
   `.claude/skills/review-models/decision-log.md`.

The first run of this skill seeds the initial default map as part of this work.

### Status and hooks

The prompt hook's engine-state line and `/engine-status` show OpenRouter with spend vs caps when
enabled. Hook output stays offline (reads state/cache only).

## Decided by the owner

- OpenRouter, not OpenCode. Claude/OpenAI models via OpenRouter are allowed.
- Regular lane when available. Full third engine (approach A).
- Setup offers integration with a step-by-step key + limits walkthrough from a yearly USD
  allowance, proposing daily/weekly/monthly limits.
- The model-review process is a reusable skill in this repo used to update the plugin, not
  shipped with it.
- Mid-run overspend pauses and asks; never kills on its own. Stop uses stream cancellation
  where the provider supports it; provider choice ignores cancellation support.
- Reviewer = best model for the tier by capability, any engine, never weaker than the
  implementer; same-model safeguard picks the next-best comparable model.
- A fifth tier `supercritical` for tasks needing frontier reasoning (not size): the top model
  reasons, maps out tasks and reviews, and never implements; tasks are implemented on their own
  tiers, at most `critical`. The user picks the top model's effort per task; no default to max.
  In auto mode the answer for a parent supercritical task carries to its supercritical children;
  outside auto mode every supercritical task asks. Headless runs in auto mode default to
  `medium`.
- The owner supplies an OpenRouter key (`OPENROUTER_API_KEY` in `~/.bashrc`) when the build
  reaches the smoke-test step.

## My calls (owner can override)

- Run OpenRouter models through the Codex CLI with per-run provider overrides.
- Cap multipliers: monthly = yearly/12 (hard, on the key), weekly = 2× average week,
  daily = 3× average day.
- Pacing against month-to-date allowance drives `remaining_pct`.
- Default `engines.openrouter: false`; key in `OPENROUTER_API_KEY`.
- OpenRouter critical implementation off by default.
- Pace term floors at 1 % (ahead of pace = `near_limit`, never `limited`).
- Fallback order and the model-centric map as defined under Routing.
- The cached `/models` list is the only availability check for OpenRouter slugs.
- `supercritical` sits linearly above `critical` and inherits its safeguards; reached by model
  classification, explicit request or re-tier after a failed critical attempt; no keyword floor.
- One effort question per supercritical task, covering its planning and review.
- Unknown permission mode counts as auto mode off (the safer side: ask).

## Left to the builder

- Exact refactor of `_quota_route` / `_balanced` into N-engine form, and whether the
  `codex-reviewer` agent is generalized or an `openrouter-reviewer` agent is added.
- `smart-task.js` changes to dispatch the OpenRouter wrapper.
- Cache file names/formats, the pace floor value, retry details for the in-flight error.
- Test layout, including a fake OpenRouter key endpoint for the wrapper tests.
