# Routing policy

`scripts/route_engine.py` implements this table. It is model-centric: `models` is a catalog
of canonical models (capability score, name per engine), and `routing` lists, per role and
route, ranked models with an effort and an `implement` switch. Every listed model is
sufficient for its route; each engine uses the highest-ranked one it can serve. The same
model reached through two engines is still one model.

## Engine states

| State | Meaning |
|---|---|
| `healthy` | No known quota restriction. |
| `near_limit` | Remaining quota below `thresholds.near_limit_pct` (default 25%). |
| `limited` | The engine reported a usage/quota limit (wrapper exit code 42). |

The engines are `claude`, `codex` and `openrouter`. An engine disabled in `engines` is treated as unavailable. "Constrained" means
`near_limit` or `limited`.

## Task routes

| Route | Typical work | Model / effort |
|---|---|---|
| trivial | lookup, typo, rename, formatting, one-file edit, explanation | haiku / low |
| standard | feature work, tests, docs, focused refactor | sonnet / medium |
| complex | multi-file investigation, debugging, architecture, unknown root cause | sonnet / high, escalate to opus / high only if needed |
| critical | production, security, migrations, auth/payment changes, irreversible or high blast-radius work | opus / high for planning and review |
| supercritical | needs frontier reasoning beyond known solutions: novel algorithms, proofs, root causes that defeated a critical attempt | top model (e.g. fable), effort asked from the user |

## Re-tiering

The model classifies with the full conversation and rubric. The hook supplies per-tier
routes and a keyword safety floor covering only the literal prompt text; never choose below
that floor. The dispatch guard does not check architect tier; the rows guide architect use.
Once per task, the scout may propose
any route or the implementer may propose one step up or down for a clear mismatch.
Never downgrade critical. The main model switches to the new row and carries finished
work over via a handoff; complex work uses an architect if unclear, critical work always
does. When routing a separate plan task, rerun `route_engine.py` for its new route and
pass the implementer's model as `--implementation-model`. A re-tier is not a quota fallback and does not use up the
other engine's fallback attempt. Applied re-tiers are listed in `reroute_events`.

## Supercritical

The top model only reasons, maps the task into sub-tasks (each at most `critical`, each
routed and implemented on its own tier) and reviews the result; the tier has
`implement: false` on every engine. It keeps critical's safeguards (architect required,
never downgraded, human review when engines are constrained). Effort is asked from the user
before the first top-model step and recorded with `engine_state.py set-effort`. With auto
mode on, the top-most supercritical task asks once and its descendants inherit the answer
(`--parent-task-id`); with auto mode off, or when `--permission-mode` is unknown, every
supercritical task asks. A headless run inherits from an ancestor, else uses `medium` in
auto mode, else stops with `requires_human_review`.

## Implementation

Among usable engines (enabled, a listed model resolves on them, the route allows
implementation) that are `healthy`, the task hash picks one with probability proportional to
headroom (`balance.routes`: standard, complex); other routes use the home engine. A
`near_limit` or `limited` engine is skipped while a healthy candidate exists. If every
candidate is constrained, use the one with the most remaining quota, one atomic task, human
review. If none is usable: `engine: none`, human review.

Headroom = remaining % − `thresholds.near_limit_pct`. For OpenRouter, remaining % is its
budget pacing: the monthly key limit is the hard cap, and daily/weekly caps (yearly allowance
× 3/365 and × 2/52) pace spend. `fallback.<src>_to_<dst>: false` stops work moving from
`src` to `dst`. A route with `implement: false` is never implemented (critical on every
engine, supercritical always).

## Test gate

On a test failure: one fix pass on the same engine with only the failing output, then
one escalation to the other engine (or one Claude tier up), then stop and ask.

## Review

The reviewer is chosen by capability, not by engine: the best sufficient model for the
route that is at least as capable as the implementer's model (`--implementation-model`),
on whichever engine can serve it, using the same headroom and constrained rules as
implementation. Same-model safeguard: if the pick is the model that implemented (any
engine), use the next-best of comparable capability. Supercritical review uses the top
model, which never implements. Claude reviews run as `verifier`; Codex and OpenRouter
reviews as `codex-reviewer`, whose prompt names the wrapper and model. Critical reviews on
Claude use `critical_review_model` / `critical_review_effort`.

With `review.split` (default true) the main review covers correctness, efficiency and
security; `--role style_review` adds a `style-reviewer` pass (`style_review_model` /
`style_review_effort`, haiku / low) for standards, best-practice and reduction. It is
skipped for trivial tasks, when `review.split` is false (the main review then covers all
six categories) or when Claude is unavailable.

## Scout

Claude haiku / low when Claude is healthy. When Claude is constrained, another engine scouts only
complex or critical tasks (they genuinely need exploration); otherwise `engine: none`.

## Architecture

Claude opus / high when Claude is healthy (top model, asked effort, for supercritical).
Claude constrained and another engine usable: that engine.
Claude near limit and the others limited: Claude sonnet / high instead of opus, with human review.

## Fallback

1. The wrappers exit 42 only on usage-limit messages. Authentication, permission,
   network and ordinary tool errors exit 1 and are never handed off.
2. On 42: `engine_state.py mark-limit --engine <engine>`, then `write_handoff.py` with
   completed and remaining work, then re-route with `--exclude-engine <engine>` and run that
   engine's wrapper (`claude_with_fallback.sh`, `codex_with_fallback.sh`, `openrouter_run.py`)
   with the handoff file.
3. The replacement engine receives the handoff, never the original broad prompt. The
   handoff's `## Do not redo` section lists completed work.
4. Each engine is tried at most once per task. If all hit limits, stop and ask the human.
5. Handoffs live in `.claude/orchestrator/handoffs/<task-id>.md`; rerunning with the
   handoff resumes from where the previous engine stopped.

## Pause on overspend (exit 43)

If an OpenRouter run exceeds its daily or weekly cap, the runner suspends it, records the
window, spend vs cap and task in `.claude/orchestrator/openrouter-runs/<run-id>/paused.json`
and exits 43 (`OPENROUTER_PAUSED <run-id>`). 43 is not a limit and triggers no fallback:
the main session tells the user and asks continue (`openrouter_run.py --resume <run-id>`)
or stop (`--stop <run-id>`, remaining work goes through a handoff). The step in flight at
pause time may still bill. The key's monthly limit remains the hard server-side cap.
