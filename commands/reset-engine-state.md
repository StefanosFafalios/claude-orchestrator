---
description: Reset Claude/Codex orchestration quota state
allowed-tools: Bash, Read
---

If `.claude/engine-state.json` already exists, show its current contents and ask the
user to confirm before resetting. Do not reset without confirmation.

On confirmation, run:

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/engine_state.py" reset

Then show the new state.
