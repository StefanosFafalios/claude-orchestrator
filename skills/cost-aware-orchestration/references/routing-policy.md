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

## Implementation

| Claude | Codex | Engine |
|---|---|---|
| healthy | healthy | Claude at the route's tier |
| healthy | near_limit / limited | Claude |
| near_limit | healthy | Codex |
| limited | healthy / near_limit | Codex |
| constrained | constrained | engine with more remaining quota; one atomic task; human review |
| limited | limited | none; human review |

`fallback.claude_to_codex: false` stops Claude's work moving to Codex.

## Review

| Condition | Engine |
|---|---|
| both healthy, critical | Codex (independent adversarial review) |
| both healthy, complex, classifier set `requires_codex_review` | Codex |
| both healthy, complex otherwise / standard | Claude sonnet / medium |
| both healthy, trivial | Claude haiku / low |
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
