---
description: Turn the OpenRouter engine on or off for this project
argument-hint: on | off
allowed-tools: Bash
---

Argument: `$ARGUMENTS` (`on` or `off`; anything else: show this usage and stop).

- `on`: run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/engine_state.py" enable openrouter`.
- `off`: run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/engine_state.py" disable openrouter`.

Report the script's output. When it says the key is not set, point to
`/cost-aware-orchestrator:setup` for the key and budget caps. Routing picks up the change on the
next prompt.
