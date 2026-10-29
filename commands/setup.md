---
description: Check Codex and usage tracking, then set up orchestration for this project
allowed-tools: Bash, Read
---

Set up the cost-aware orchestrator. Run each check, report the result in one line, and
ask before every change. Change nothing outside `.claude/` in the project and
`~/.claude/settings.json`, and only with the user's yes.

1. **Python**: `python3 --version` (3.11+ recommended, 3.8+ works).

2. **Codex** (optional engine): run `command -v codex` and `codex login status`.
   - Missing: Codex is optional. To add it: `npm install -g @openai/codex`, then
     `codex login`. To run Claude-only, offer to set `"engines": {"codex": false}` in the
     project config.
   - Installed but not logged in: tell the user to run `! codex login`.
   - Logged in: Codex is ready. Nothing else is needed.

3. **Usage tracking**: run
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/usage_sources.py"` and show what it observed.
   - `codex: null` with Codex installed just means no Codex session has run yet.
   - `claude: null` means the status line does not save its input yet. Read
     `~/.claude/settings.json` and show the user the proposed `statusLine.command`:
     `input=$(cat); printf '%s' "$input" > ~/.claude/statusline-input.json.tmp && mv ~/.claude/statusline-input.json.tmp ~/.claude/statusline-input.json; printf '%s' "$input" | <existing command>`
     where `<existing command>` is the current command unchanged (if there is none, end
     after the `mv`, which leaves the status line blank). Show the before and after, and
     edit the file only on yes. Do not touch any other setting.

4. **Project config**: if `.claude/orchestrator.config.json` is missing, ask
   "Create .claude/orchestrator.config.json with default orchestration settings?" On yes,
   copy `${CLAUDE_PLUGIN_ROOT}/templates/orchestrator.config.json`. Then detect the test,
   lint and typecheck commands from the project's own files (`pyproject.toml`,
   `package.json` scripts, `Makefile`, CI config). Propose them, and write only the ones
   the user confirms. Leave the rest empty.

5. **Git hygiene**: if the project is a git repository and `.gitignore` lacks them, offer
   to add `.claude/engine-state.json` and `.claude/orchestrator/`.

6. Finish with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/engine_state.py" status` and a
   summary: which engines are ready, whether usage tracking is active for each, and
   which config values are still empty.
