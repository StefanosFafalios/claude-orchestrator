---
name: cost-aware-orchestration
description: Use automatically for every nontrivial coding, debugging, refactoring, testing, architecture, migration, security-sensitive, or multi-step task. Chooses the cheapest sufficient Claude model and effort, routes work between Claude and Codex based on quota state, creates resumable handoffs, and preserves independent review.
---

Always apply this skill before beginning a nontrivial task. It is the default
execution policy for this project, not an optional command.

Do not apply it to:
- Simple conversational questions
- Reading one small file
- A single trivial formatting change
- Explicit user requests to bypass orchestration

If the injected context says `ORCHESTRATION POLICY DISABLED` (project config has
`"automatic": false`), do not run this workflow automatically; use it only when the
user invokes `/cost-aware-orchestrator:smart-task`.

`<scripts>` below is `${CLAUDE_PLUGIN_ROOT}/scripts`; the injected context prints its
absolute path as `Plugin scripts:`.

Workflow:

1. Read `.claude/orchestrator.config.json` if present. If it is missing, ask once per
   session: "Create .claude/orchestrator.config.json with default orchestration settings?"
   Only on yes, copy `${CLAUDE_PLUGIN_ROOT}/templates/orchestrator.config.json` there,
   leave test/lint/typecheck empty until detected or configured, and touch nothing
   outside `.claude/`. On no, continue with plugin defaults.
2. Classify the task (write the task text to a file under `.claude/orchestrator/runs/`):
   python3 "<scripts>/classify_task.py" --task-file <task-file>
3. Check engine state:
   python3 "<scripts>/engine_state.py" status
4. Choose engine, model, and effort:
   python3 "<scripts>/route_engine.py" --role implementation --task-route <route>
   (add `--requires-codex-review` when the classifier set it).
   Any script exiting 2 means invalid configuration: report the error and stop.
5. Use the selected Claude model and effort for normal work. Trivial tasks: do them
   directly, no subagents, no review fan-out.
6. Use the `scout` agent for repository exploration.
7. Use the `implementer` agent for standard implementation.
8. Use the `architect` agent before implementation for complex or critical work.
9. For review, run `route_engine.py --role review` and choose an engine different from
   the implementer when both are healthy (`verifier` on Claude, `codex-reviewer` on Codex).
10. If an engine exits with code 42, mark it limited
    (`engine_state.py mark-limit --engine <engine> --message "<first line>"`), create a
    handoff with `write_handoff.py` listing completed and remaining work, and delegate
    only the remaining work to the other engine (`codex_with_fallback.sh` or
    `claude_with_fallback.sh` with the handoff file). Retry once per engine per task.
11. Never delegate authentication, permission, network, or ordinary tool errors as quota
    failures. Exit code 1 is an ordinary failure: report it.
12. If both engines are constrained, stop fan-out and require human approval.
13. After completion, run `engine_state.py record-result` for each engine used and save
    the final summary to `.claude/orchestrator/results/<task-id>.md`.

All delegated work follows `references/coding-standards.md`. The full routing table is
in `references/routing-policy.md`.
