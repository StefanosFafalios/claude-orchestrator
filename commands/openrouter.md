---
description: Turn the OpenRouter engine on or off for this project
argument-hint: on | off
allowed-tools: Bash
---

Argument: `$ARGUMENTS` (`on` or `off`; anything else: show this usage and stop).

- `on`: run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/engine_state.py" enable openrouter`.
  Then check only whether `OPENROUTER_API_KEY` is set (`test -n "$OPENROUTER_API_KEY"`; never
  print it). If it is not set, say OpenRouter stays unused until the key is in the environment,
  and point to `/cost-aware-orchestrator:setup` for the key and budget caps.
- `off`: run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/engine_state.py" disable openrouter`.

Report the script's one-line result. Routing picks up the change on the next prompt.
