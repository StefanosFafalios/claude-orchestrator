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
   python3 "<scripts>/route_engine.py" --role implementation --task-route <route> --task-id <task-id>
   (add `--requires-codex-review` when the classifier set it). The task id enables the
   adaptive split: healthy engines share standard and complex work in proportion to their
   headroom, so `engine: codex` is a normal outcome, not only a fallback. Delegate it.
   Any script exiting 2 means invalid configuration: report the error and stop.
5. Use the selected Claude model and effort for normal work. Trivial tasks: do them
   directly, no subagents, no review fan-out.
6. Use the `scout` agent for repository exploration.
7. Use the `implementer` agent for standard implementation.
8. Use the `architect` agent before implementation for complex or critical work.
9. When executing a written plan, including Superpowers `executing-plans` or
   `subagent-driven-development`, treat each plan task as its own task: classify and
   route it with `route_engine.py --task-id <plan-task-id>`, implement with the
   orchestrator's `implementer` agent or Codex handoff as routed, review with
   `verifier`/`codex-reviewer` on the other engine, and apply the exit-42 fallback.
   Do not dispatch generic subagents with the session model for plan tasks;
   brainstorming and plan writing stay in the session.
10. Re-tier at most once per task, as in `references/routing-policy.md#re-tiering`: rerun
   `route_engine.py` with the scout's route or one step up/down from the implementer
   (never down from critical), plan if it became complex/critical, and hand off.
11. Run the configured test command. On failure, give the same engine one fix pass with
   only the failing output (via a handoff); if it still fails, hand it once to the other
   engine (or one Claude tier up), then stop and ask.
12. For review, run `route_engine.py --role review --implementation-engine <engine>`: it
   puts the review on the other engine when both are healthy (`verifier` on Claude,
   `codex-reviewer` on Codex).
13. If an engine exits with code 42, mark it limited
    (`engine_state.py mark-limit --engine <engine> --message "<first line>"`), create a
    handoff with `write_handoff.py` listing completed and remaining work, and delegate
    only the remaining work to the other engine (`codex_with_fallback.sh` or
    `claude_with_fallback.sh` with the handoff file). Retry once per engine per task.
14. Never delegate authentication, permission, network, or ordinary tool errors as quota
    failures. Exit code 1 is an ordinary failure: report it.
15. If both engines are constrained, stop fan-out and require human approval.
16. After completion, run `engine_state.py record-result` for each engine used and save
    the final summary to `.claude/orchestrator/results/<task-id>.md`.

All delegated work follows `references/coding-standards.md`. The full routing table is
in `references/routing-policy.md`.
