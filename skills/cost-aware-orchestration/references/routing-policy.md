# Routing policy

`scripts/route_engine.py` implements this table. Model and effort names come from
`defaults` in the merged configuration; the values shown are the plugin defaults.

## Engine states

| State | Meaning |
|---|---|
| `healthy` | No known quota restriction. |
| `near_limit` | Remaining quota below `thresholds.near_limit_pct` (default 25%). |
| `limited` | The engine reported a usage/quota limit (wrapper exit code 42). |

An engine disabled in `engines` is treated as unavailable. "Constrained" means
`near_limit` or `limited`.

## Task routes

| Route | Typical work | Model / effort |
|---|---|---|
| trivial | lookup, typo, rename, formatting, one-file edit, explanation | haiku / low |
| standard | feature work, tests, docs, focused refactor | sonnet / medium |
| complex | multi-file investigation, debugging, architecture, unknown root cause | sonnet / high, escalate to opus / high only if needed |
| critical | production, security, migrations, auth/payment changes, irreversible or high blast-radius work | opus / high for planning and review |

## Codex tiers

Each route has a Codex equivalent in `codex.tiers` (model, effort, `implement` switch).
Defaults: trivial `gpt-6-luna`/low, standard `gpt-6.1-terra`/medium, complex
`gpt-6.1-sol`/high, critical `gpt-6-astra`/high with `implement: false`. Critical work is
never implemented on Codex, but Codex may still review it.

## Implementation

| Claude | Codex | Engine |
|---|---|---|
| healthy | healthy | adaptive split for `balance.routes` (standard, complex): Codex share = Codex headroom ÷ total headroom, by task hash; otherwise Claude |
| healthy | near_limit / limited | Claude |
| near_limit | healthy | Codex at the equivalent tier, if the tier allows it |
| limited | healthy / near_limit | Codex at the equivalent tier, if the tier allows it |
| constrained | constrained | engine with more remaining quota; one atomic task; human review |
| limited | limited | none; human review |

Headroom = remaining % − `thresholds.near_limit_pct`. `fallback.claude_to_codex: false`
stops Claude's work moving to Codex. A tier with `implement: false` and Claude
unavailable returns `engine: none`.

## Test gate

On a test failure: one fix pass on the same engine with only the failing output, then
one escalation to the other engine (or one Claude tier up), then stop and ask.

## Review

| Condition | Engine |
|---|---|
| both healthy, implementer known | the other engine (cross-engine review) |
| both healthy, critical | Codex (independent adversarial review) |
| both healthy, complex, classifier set `requires_codex_review` | Codex |
| both healthy otherwise | Claude: sonnet / medium (trivial: haiku / low) |
| Codex constrained | Claude |
| Claude constrained | Codex |
| both constrained | more remaining quota; human review |

Critical reviews on Claude use `critical_review_model` / `critical_review_effort`.

## Scout

Claude haiku / low when Claude is healthy. When Claude is constrained, Codex scouts only
complex or critical tasks (they genuinely need exploration); otherwise `engine: none`.

## Architecture

Claude opus / high when Claude is healthy. Claude constrained and Codex usable: Codex.
Claude near limit and Codex limited: Claude sonnet / high instead of opus, with human review.

## Fallback

1. The wrappers exit 42 only on usage-limit messages. Authentication, permission,
   network and ordinary tool errors exit 1 and are never handed off.
2. On 42: `engine_state.py mark-limit`, then `write_handoff.py` with completed and
   remaining work, then run the other engine's wrapper with the handoff file.
3. The replacement engine receives the handoff, never the original broad prompt. The
   handoff's `## Do not redo` section lists completed work.
4. Each engine is tried at most once per task. If both hit limits, stop and ask the human.
5. Handoffs live in `.claude/orchestrator/handoffs/<task-id>.md`; rerunning with the
   handoff resumes from where the previous engine stopped.
