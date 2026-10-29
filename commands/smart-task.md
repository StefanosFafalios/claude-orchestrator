---
description: Run a cost-aware, quota-aware implementation workflow
argument-hint: [task]
allowed-tools: Read, Grep, Glob, Bash, Write, Workflow
---

Run the cost-aware orchestration workflow for:

$ARGUMENTS

Steps:
1. Ensure `.claude/orchestrator.config.json` exists; if not, ask
   "Create .claude/orchestrator.config.json with default orchestration settings?" and on
   yes copy it from `${CLAUDE_PLUGIN_ROOT}/templates/orchestrator.config.json`
   (on no, continue with plugin defaults). Do not modify files outside `.claude/`.
2. Classify the task: write the task text to `.claude/orchestrator/runs/task.txt`, then run
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/classify_task.py" --task-file .claude/orchestrator/runs/task.txt`.
3. Check engine state: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/engine_state.py" status`.
   If either script exits 2 (invalid config), report the error and stop.
4. Run the workflow. Call it by name; if the tool does not know the name, pass
   `scriptPath: "${CLAUDE_PLUGIN_ROOT}/workflows/smart-task.js"` instead:

   ```
   Workflow({
     name: "cost-aware-orchestrator:smart-task",
     args: {
       task: "<the task text>",
       timestamp: "<output of: date -u +%Y%m%dT%H%M%SZ>",
       plugin_root: "${CLAUDE_PLUGIN_ROOT}",
       classification: <JSON from step 2>
     }
   })
   ```

   If the Workflow tool is not available in this session, run the same steps inline
   instead, following the `cost-aware-orchestration` skill: route with
   `route_engine.py`, dispatch the `scout`, `architect`, `implementer` and
   `verifier`/`codex-reviewer` agents with the routed model and effort, run the
   configured test command, and apply the same exit-42 fallback and handoff rules.
   Report that the run was inline.

   To resume after an interruption, relaunch with `resumeFromRunId`, or pass
   `handoff: ".claude/orchestrator/handoffs/<task-id>.md"` and the original `task_id`
   to continue from a saved handoff.
5. Save the returned JSON as `.claude/orchestrator/results/<task_id>.md` (a short
   summary followed by the JSON in a fenced block).
6. Report the selected engine, model, effort, files changed, test results,
   review findings, and any fallback event. If `requires_human_review` is true, say so first.
