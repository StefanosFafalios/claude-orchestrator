---
description: Show Claude, Codex and OpenRouter quota/orchestration state
allowed-tools: Bash
---

Run:

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/engine_state.py" status --check-login

and, for the current routing decisions:

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/route_engine.py" --role implementation --task-route standard
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/route_engine.py" --role review --task-route critical

If `engines.openrouter` is true, also run
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/openrouter_budget.py" verify` and show spend vs the
daily/weekly/monthly caps (and any `matches: false` mismatch). List pending pauses with
`ls .claude/orchestrator/openrouter-runs/*/paused.json`; for each, read it and show the
window, spend vs cap and task, with the `openrouter_run.py --resume <run-id>` / `--stop <run-id>`
choice.

Then explain:
- Which engine should handle implementation
- Which engine should handle review
- Whether fallback is active (an engine is `near_limit` or `limited`) or an OpenRouter run is paused
- What the user should do if all engines are constrained: wait for the quota reset shown
  in `last_error`, approve a single atomic task, or run
  `/cost-aware-orchestrator:reset-engine-state` once quota is restored.

To record quota you can see in `/usage` or the Codex status line, the user can run
`engine_state.py set --engine claude --remaining-pct 20`.
