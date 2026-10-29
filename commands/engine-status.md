---
description: Show Claude and Codex quota/orchestration state
allowed-tools: Bash
---

Run:

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/engine_state.py" status

and, for the current routing decisions:

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/route_engine.py" --role implementation --task-route standard
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/route_engine.py" --role review --task-route critical

Then explain:
- Which engine should handle implementation
- Which engine should handle review
- Whether fallback is active (an engine is `near_limit` or `limited`)
- What the user should do if both engines are constrained: wait for the quota reset shown
  in `last_error`, approve a single atomic task, or run
  `/cost-aware-orchestrator:reset-engine-state` once quota is restored.

To record quota you can see in `/usage` or the Codex status line, the user can run
`engine_state.py set --engine claude --remaining-pct 20`.
