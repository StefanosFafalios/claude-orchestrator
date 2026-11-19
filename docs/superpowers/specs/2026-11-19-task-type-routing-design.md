# Task-type routing — design

Issue: #1. Owner-approved 2026-11-19.

## Intent

The router picks models by role × complexity tier. Models differ by kind of work, so a
cheaper or stronger model for a given kind should be preferred where evidence shows it.
Goal unchanged: lower cost and token use without losing quality.

## Goals, non-goals, anti-goals

- Goals: optional per-type overrides of a role × tier row; type detected at no extra call;
  configs without type rows behave exactly as today.
- Non-goals: per-type capability scores; changing the reviewer-strength rule; seeding type
  rows without per-type benchmark evidence.
- Anti-goals: extra tokens on every prompt when no type rows exist; a type row that weakens
  review; asking the owner to pick per-type models (decisions are rule-based).

## Approaches

1. **Nested `types` inside a row (picked).** `routing.<role>.<tier>.types.<type>` holds a
   full row (`models`, `effort`, optional `model_effort`). Overrides exactly the role × tier
   it names, validation reuses the row checks, and absence means today's behavior.
2. Separate top-level `routing_by_type.<type>.<role>.<tier>`. Same power, but a second tree to
   validate and keep in sync with `routing`; rejected.
3. Per-type model scores feeding the reviewer rule. Needs per-type evidence for every model
   and changes review safety; rejected (non-goal).

## Design

- **Types:** fixed set `frontend`, `debug`, `refactor`, `tests`, `docs`; anything else
  (missing, unknown, `general`) means "no type" and uses the tier row.
- **Config:** a row may contain `"types": {"<type>": {"models": [...], "effort": "...",
  "model_effort": {...}?}}`. Keys must be in the fixed set; each value is validated like a
  row (known models, valid effort, `model_effort` keys within its own models; `ask` only
  where `ask` is allowed for the parent row). Invalid → exit 2. The template ships none.
- **Routing:** `model_map.row(config, role, route, task_type=None)` returns the type row when
  the parent row has it, else the parent row. `route()` and `route_engine.py --task-type`
  pass it through. Everything after row lookup is unchanged: effort precedence (user-named
  supercritical > ask > `model_effort` > row effort), reviewer ≥ every writer by overall
  score and never the same model, fallbacks, re-tiering. Review routing uses the task's
  type for the review row too.
- **Detection:** the decider asks for `"type"` in the same JSON; it is validated against
  the set (else omitted) and printed in its output. Without the decider, the session model
  picks the type from a one-line list that the hook prints only when the config has at
  least one type row. The keyword classifier never sets a type.
- **Hook:** when type rows exist, after the tier rows print one compact line per tier that
  has type overrides, listing only the differing entries (e.g.
  `complex/frontend: implement=implementer/sonnet/high`). Nothing is printed otherwise.
- **Workflow:** `classification` may carry `type`; the workflow passes `--task-type` on every
  route call; the decider's `type` is used when the workflow runs the decider itself.
- **Model review skill:** add a Fit rule: a type row only from per-type benchmark evidence,
  cheapest-sufficient first, same reviewer coverage rule as tier rows; no evidence, no row.

## Left to the builder

Exact hook line wording within the existing size caps, helper names, test layout.
