# Routing policy

`scripts/route_engine.py` implements this table. It is model-centric: `models` is a catalog
of canonical models (capability score, name per engine), and `routing` lists, per role and
route, ranked models with an effort. Every listed model is sufficient for its route; each
engine uses the highest-ranked one it can serve. The same model reached through two engines
is still one model, so a constrained engine's work can move to another engine that serves
the same model with no loss of quality.
Rows may contain type sub-rows; for a clear type state `route=<tier>/<type>` and pass
`--task-type <type>`. Unknown types use the tier row, and the reviewer capability and
different-model rules apply unchanged.

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
| supercritical | needs frontier reasoning beyond known solutions: novel algorithms, proofs, root causes that defeated a critical attempt | top model (e.g. fable) / medium; user-named effort overrides |

## Re-tiering

The model classifies with the full conversation and rubric. The hook supplies per-tier
routes and a keyword safety floor covering only the literal prompt text; never choose below
that floor. When OpenRouter is set up, healthy and within budget, a cheap model on the
`routing.decider` row classifies the prompt first (`decide_route.py`, clamped to the keyword
floor and capped at `critical`: a `supercritical` answer is only a suggestion in its reason). It sees only the prompt, so its route is a further floor: the session model raises it
from conversation context, never lowers it, and an approval keeps the approved tier. Its exit 3
on any failure returns classification to the session model. When it reports `ask: true`
(confidence below `decider.min_confidence`), the session asks the user to choose between its
`route` and `alternative`.
The dispatch guard does not check architect tier; the rows guide architect use.
Once per task, the scout may propose
any route or the implementer may propose one step up or down for a clear mismatch.
Never downgrade critical. The main model switches to the new row and carries finished
work over via a handoff; complex work uses an architect if unclear, critical work always
does. When routing a separate plan task, rerun `route_engine.py` for its new route and
pass the implementer's model as `--implementation-model`. A re-tier is not a quota fallback and does not use up the
other engine's fallback attempt. Applied re-tiers are listed in `reroute_events`.

## Supercritical

The top model only reasons, maps the task into sub-tasks (each at most `critical`, each
routed and implemented on its own tier) and reviews the result; it never implements
(supercritical implementation uses the `critical` row). It keeps critical's safeguards (architect required,
never downgraded, human review when engines are constrained). Effort defaults to `medium`;
an effort the user names with `engine_state.py set-effort` overrides it for that task and
its descendants. Configurations with an opt-in `ask` row retain the ask flow.

Effort precedence: a user-named supercritical effort, then the `ask` flow, then the row's
optional `model_effort` entry for the picked model, then the row's `effort`. A user-named
effort applies to whichever model the row picks, including a fallback from the top model.

## Implementation

Among usable engines (enabled, and a listed model resolves on them) that are `healthy`, the task hash picks one with probability proportional to
headroom (`balance.routes`: standard, complex); other routes use the home engine. A
healthy Claude or Codex candidate takes precedence over OpenRouter for the same canonical model.
`near_limit` or `limited` engine is skipped while a healthy candidate exists. If every
candidate is constrained, use the one with the most remaining quota, one atomic task, human
review. If none is usable: `engine: none`, human review.

Headroom = remaining % − `thresholds.near_limit_pct`. For OpenRouter, remaining % is its
budget pacing: the monthly key limit is the hard cap, and daily/weekly caps (yearly allowance
× 3/365 and × 2/52) pace spend. `fallback.<src>_to_<dst>: false` stops work moving from
`src` to `dst`. Critical implementation lists only the strongest implementing model
(opus), so it never moves to a weaker model: while Claude is constrained it runs on another
engine only when that engine serves the same model (OpenRouter's `anthropic/claude-opus-…`),
else it stops with `engine: none` and human review.

## Test gate

On a test failure: one fix pass on the same engine with only the failing output, then
one escalation to another engine at the same route (or the architecture model when no
other engine can take it), then stop and ask. The review that follows excludes every model
that wrote code.

## Review

The reviewer is chosen by capability, not by engine: the first model in the route's
row (rows are ordered cheapest first) that is at least as capable as the implementer's model (`--implementation-model`),
on whichever engine can serve it, using the same headroom and constrained rules as
implementation. Same-model safeguard: a model that implemented (any engine), including a
fix-pass escalation or a fallback model, is skipped; pass each as a repeated
`--implementation-model`, and the reviewer must be at least as capable as the strongest. Trivial tasks get no model review: the tests and lint gate them. Supercritical review uses the top
model, which never implements. Claude reviews run as `verifier`; Codex and OpenRouter
reviews as `codex-reviewer`, whose prompt names the wrapper and model.

With `review.split` (default true) the main review covers correctness, efficiency and
security; `--role style_review` adds a `style-reviewer` pass (the `routing.style_review`
row, cheapest first at low effort) for standards, best-practice and reduction. It is
skipped for trivial tasks, when `review.split` is false (the main review then covers all
six categories) or when its routed engine is constrained. Off Claude it runs as
`codex-reviewer` through the engine's wrapper; exit 42 marks that engine limited and re-routes.

## Scout

The cheapest model in the scout row, low effort, on the first healthy engine that serves one;
off Claude it runs read-only through the engine's wrapper. A constrained pick (one atomic
task, human review) is skipped rather than spending scarce quota. Trivial tasks get no scout.

## Docs

Last, once a nontrivial task passed its tests and review without needing a human,
`--role docs` (the `style_review` models and effort when the config has no `docs` row)
runs `doc-updater`: it checks whether the change altered behavior, commands, paths,
conventions or workflows that the AI instruction docs (`AI_CONTEXT.md`, `CLAUDE.md`,
`AGENTS.md`, project skills and agents) describe, and makes the smallest accurate edit or
none. Off Claude it runs through the engine's wrapper with `workspace-write` and the agent's
rules in the prompt; exit 42 marks that engine limited and re-routes; exit 43 asks a human.
A constrained pick is skipped.

## Architecture

Claude first while healthy: sonnet / high for trivial and standard (used only to escalate a
failed fix), opus / high for complex and critical, the top model / medium for supercritical.
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
