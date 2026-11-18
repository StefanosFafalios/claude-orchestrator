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
2. Classify the task once and pass the result as `classification: {"route": "<tier>",
   "reason": "<why>"}`. If the hook context told you to pipe the prompt to `decide_route.py`,
   use that run (do not run it again): its route is a floor you raise from the conversation
   when warranted, never lower. Otherwise, or on its exit 3, classify by the hook rubric with
   the whole conversation, never below the keyword floor. (Callers that pass no
   `classification` get the workflow's own decider, then the keyword classifier.)
3. Check engine state: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/engine_state.py" status`.
   If it exits 2 (invalid config), report the error and stop.
4. Run the workflow. Call it by name; if the tool does not know the name, pass
   `scriptPath: "${CLAUDE_PLUGIN_ROOT}/workflows/smart-task.js"` instead:

   ```
   Workflow({
     name: "cost-aware-orchestrator:smart-task",
     args: {
       task: "<the task text>",
       timestamp: "<output of: date -u +%Y%m%dT%H%M%SZ>",
       plugin_root: "${CLAUDE_PLUGIN_ROOT}",
       classification: <JSON from step 2>,
       permission_mode: "<mode>",
       parent_task_id: "<parent task id>"
     }
   })
   ```

   `permission_mode` is the mode shown on the hook context's supercritical line
   (`--permission-mode <mode>`); omit it when unknown (the workflow treats missing as
   `default`, auto mode off). Pass `parent_task_id` only when this task is a sub-task of a
   supercritical parent: it lets the workflow inherit the parent's effort answer in auto
   mode. Without them a supercritical run always stops for a human.

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
   review findings, and any fallback event. If `requires_human_review` is true, say so first;
   on a paused OpenRouter run (exit 43) show the run id and its resume/stop commands.
