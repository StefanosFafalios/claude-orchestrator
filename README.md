# claude-orchestrator

A Claude Code plugin (`cost-aware-orchestrator`) that makes Claude Code spend tokens
and quota deliberately:

- **Cost-aware routing**: each task is classified as trivial, standard, complex or
  critical by a deterministic keyword classifier (no LLM call), then routed to the
  cheapest sufficient Claude model and effort.
- **Tiered subagents**: `scout` (haiku/low), `implementer` (sonnet/medium),
  `architect` (opus/high), `verifier` (sonnet/medium), `style-reviewer` (haiku/low), and `codex-reviewer`.
- **Optional Codex integration** through your locally installed Codex CLI.
- **Bidirectional quota fallback**: when Claude is near or at its limit, the remaining
  work goes to Codex, and vice versa. Review stays on the healthier engine.
- **Resumable handoffs**: the replacement engine gets a handoff listing completed work
  under `## Do not redo`, never the original broad prompt.
- **Automatic activation**: on every prompt, a hook classifies the request locally
  (no network, a few milliseconds) and injects the policy plus a concrete plan for that
  prompt: which agents, models and engines to use. A broadly scoped skill backs it up.
  You don't need a slash command. Subagent reports are not re-classified.
  Written plans, including Superpowers plans, are routed per task during execution.

- **Usage tracking**: reads remaining Claude and Codex quota from local files (status
  line input, Codex session logs) and routes before an engine runs out.

No API keys, OAuth tokens or secrets are stored. Network access only happens through
your local `claude` and `codex` CLIs.

## Requirements

- Claude Code with plugin support. `/smart-task` uses the Workflow tool when your
  account has it; otherwise it runs the same steps inline with regular subagents.
- `python3` (3.11+ recommended; the scripts also run on 3.8+), `bash`, `git`
- Optional: the [Codex CLI](https://github.com/openai/codex), logged in (`codex login`)

## Installation

```text
/plugin marketplace add StefanosFafalios/claude-orchestrator
/plugin install cost-aware-orchestrator@claude-orchestrator
```

For local development: `claude --plugin-dir /path/to/claude-orchestrator`.

## Setup

Run once per project:

```text
/cost-aware-orchestrator:setup
```

It checks Python and the Codex CLI, turns on usage tracking, creates the project config
(detecting your test, lint and typecheck commands) and offers the `.gitignore` entries.
It asks before every change.

**Codex** is optional. If the `codex` CLI is installed and logged in (`codex login`), the
plugin uses it with no further configuration. Without it, set
`"engines": {"codex": false}` and everything stays on Claude.

## Usage tracking

Routing uses real quota numbers when they are available locally (no network):

| Engine | Source |
|---|---|
| Codex | the newest session log in `~/.codex/sessions/` (`rate_limits`: 5-hour and weekly windows) |
| Claude | `~/.claude/statusline-input.json`, saved by your status line (`rate_limits.five_hour` / `seven_day`) |

The prompt hook, `route_engine.py` and `engine_state.py status` sync these on every
call. The tightest active window sets `remaining_pct`. A window past its reset time
counts as unused, so a `limited` engine recovers on its own. A newer manual
`engine_state.py set` or `mark-limit` wins over an older observation. For Claude,
`/cost-aware-orchestrator:setup` adds one save step in front of your existing status line
command. Turn syncing off with `"usage_sync": false`.

## Per-project setup

On the first nontrivial task in a project without config, Claude asks:
"Create .claude/orchestrator.config.json with default orchestration settings?" Say yes
to copy `templates/orchestrator.config.json`, then fill in your commands:

```json
{
  "commands": { "test": "pytest -q", "lint": "ruff check .", "typecheck": "mypy src" }
}
```

Project config overrides only the fields it contains. Everything else comes from the
plugin defaults. Unknown fields are ignored. Invalid JSON stops orchestration with a
clear error.

Add the runtime state to the project's `.gitignore`:

```gitignore
.claude/engine-state.json
.claude/orchestrator/
```

## Example commands

```text
/cost-aware-orchestrator:setup
/cost-aware-orchestrator:smart-task Implement retry handling for webhook failures and update tests
/cost-aware-orchestrator:engine-status
/cost-aware-orchestrator:reset-engine-state
```

Without a command, the policy still applies:

| Prompt | Behavior |
|---|---|
| "Fix this typo" | Claude haiku/low, no fan-out |
| "Add tests for this module" | sonnet/medium with a focused implementer |
| "Debug this intermittent failure" | scout, then sonnet/high (opus/high if needed) |
| "Refactor authentication and add tests" | critical: architect plan, healthy engine implements, other engine reviews |
| "What does this function do?" | answered directly; no workflow |

## Routing policy

| Route | Examples | Claude tier | Codex equivalent | Codex may implement |
|---|---|---|---|---|
| trivial | typo, rename, lookup | haiku / low | `gpt-6-luna` / low | yes (inline on Claude by default) |
| standard | feature, tests, docs | sonnet / medium | `gpt-6.1-terra` / medium | yes |
| complex | debugging, multi-file | sonnet / high, escalate to opus | `gpt-6.1-sol` / high | yes |
| critical | production, security, migrations, auth/payment | opus / high | `gpt-6-astra` / high (review only) | **no**: stays on Claude |

**Adaptive split.** When both engines are healthy, standard and complex tasks are
shared in proportion to each engine's headroom (remaining quota above the near-limit
line). For example, Claude at 90% and Codex at 45% gives Codex about 24% of tasks. As
one subscription gets closer to its limit, more work goes to the other. The choice is a
deterministic hash of the task, so the same task always routes the same way.

**Re-tiering.** The keyword route is a free first guess; the Haiku scout or the
implementer may correct it once per task (critical is never downgraded).

**Cross-engine review.** Review always runs on the engine that did not implement, when
it is healthy: Claude work is reviewed by Codex, and Codex work by Claude.
The main review covers correctness, efficiency and security; a cheap haiku `style-reviewer`
covers standards, best practices and code/text reduction (set `review.split` to false to merge them).

**Test gate.** If the project's tests fail, the same engine gets one fix pass with only
the failing output. If they still fail, the other engine (or one Claude tier up) gets
one try, then it stops and asks you.

When an engine is constrained (near limit or limited), its work goes to the other
engine at the equivalent tier, unless that tier's `implement` switch is off. When both
are constrained, the engine with more remaining quota does one atomic task and a human
reviews. Full table:
[`skills/cost-aware-orchestration/references/routing-policy.md`](skills/cost-aware-orchestration/references/routing-policy.md).

Tune it per project:

```json
{
  "codex": { "tiers": { "standard": { "model": "gpt-6.1-sol", "effort": "medium", "implement": true } } },
  "balance": { "mode": "adaptive", "routes": ["standard", "complex"] }
}
```

Set `"balance": {"mode": "off"}` to keep Claude as the primary engine, so Codex works
only as a fallback and reviewer.

## Fallback behavior

1. `scripts/claude_with_fallback.sh` and `scripts/codex_with_fallback.sh` exit **42**
   only on usage-limit messages (`usage limit`, `hit your session/weekly limit`,
   `limit reached`, and `resets`/`upgrade` when no auth or network error is present).
   Authentication, permission, network and ordinary errors exit 1 and are never handed off.
2. On 42, the engine is marked `limited` in `.claude/engine-state.json`. A handoff is
   written to `.claude/orchestrator/handoffs/<task-id>.md` (objective, completed and
   remaining work, diff, failing tests, project commands, constraints, output contract).
   Only the remaining work goes to the other engine.
   `claude_with_fallback.sh` runs Claude with `--permission-mode acceptEdits` by default
   (edits allowed, shell commands such as tests need approval). Set
   `CLAUDE_PERMISSION_MODE` to change it.
3. Each engine is tried at most once per task. If both hit limits, orchestration stops
   and asks you.
4. To resume an interrupted run, relaunch the workflow with `resumeFromRunId`, or pass
   the saved handoff (`handoff: ".claude/orchestrator/handoffs/<id>.md"`).

Usage tracking (above) switches engines before quota runs out. Without it, record what
`/usage` shows yourself:

```bash
python3 "$PLUGIN/scripts/engine_state.py" set --engine claude --remaining-pct 20   # → near_limit
```

**Codex → Claude**: to let a Codex session hand off to Claude, add this to the project's
`AGENTS.md`:

```markdown
If Codex reports a usage limit mid-task, write a handoff with
`python3 <plugin>/scripts/write_handoff.py --target-engine claude ...` and run
`bash <plugin>/scripts/claude_with_fallback.sh <handoff>`. Exit 42 means Claude is limited too: stop.
```

## Security rules

- No credentials, tokens or user-specific config in the repository or in state.
- Error messages, diffs and test output are scrubbed of credential-shaped strings
  before they are persisted or handed off.
- Delegated Codex runs default to `--sandbox read-only`. Only implementation uses
  `workspace-write`.
- Hooks read local files only and never call `claude`, `codex` or the network.
- The dispatch guard only *denies*. It never auto-approves, so your permission rules
  still apply.
- All engines follow
  [`references/coding-standards.md`](skills/cost-aware-orchestration/references/coding-standards.md):
  smallest diff, no commits unless asked, and no agent attribution
  (`Co-Authored-By`, "Generated with") in commits or PRs.

## Updating thresholds

```json
{ "thresholds": { "near_limit_pct": 30 } }
```

An engine whose `remaining_pct` is below `near_limit_pct` is treated as `near_limit`.
Models and efforts are overridable the same way under `defaults`, e.g.
`{"defaults": {"implementation_model": "opus"}}`.

## Disabling Codex

```json
{ "engines": { "codex": false } }
```

All routing then stays on Claude. To keep Codex for review only, set
`"fallback": {"claude_to_codex": false}`. To turn off automatic orchestration (the hook
then injects `ORCHESTRATION POLICY DISABLED`, and `/smart-task` still works):

```json
{ "engines": { "claude": false, "codex": false }, "automatic": false }
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `error: invalid JSON in .../orchestrator.config.json` | Fix the reported line and column. Orchestration stays stopped until the file parses |
| Stuck on Codex after the Claude quota reset | `/cost-aware-orchestrator:reset-engine-state`, or `engine_state.py set --engine claude --remaining-pct 100` |
| Dispatch denied by `[cost-aware-orchestrator]` | Follow the reason, or add a `Justification:` line for a standard task that needs the architect |
| Codex wrapper exits 1 with "not logged in" | Run `codex login`. This is not a quota failure, so there is no fallback |
| Hook seems silent | Check `claude --debug`. The hook prints JSON with `hookSpecificOutput.additionalContext` |

## Running tests

```bash
python3 -m pytest -q        # offline; workflow scenarios need node
ruff check .
shellcheck scripts/*.sh     # if installed
```

The tests never invoke the real `claude` or `codex`. The wrappers are exercised with
fake CLIs, and `workflows/smart-task.js` runs under `tests/workflow_harness.mjs` with
mocked agents.

## License

MIT
