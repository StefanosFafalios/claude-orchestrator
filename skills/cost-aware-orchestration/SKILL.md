---
name: cost-aware-orchestration
description: Use automatically for every nontrivial coding, debugging, refactoring, testing, architecture, migration, security-sensitive, or multi-step task. Chooses the cheapest sufficient Claude model and effort, routes work between Claude, Codex and OpenRouter models based on quota and budget, creates resumable handoffs, and preserves independent review.
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
2. Classify using the full conversation and the hook rubric; the hook supplies per-tier
   routes and a keyword safety floor. Never choose below the floor.
3. Check engine state:
   python3 "<scripts>/engine_state.py" status
4. Choose engine, model, and effort:
   python3 "<scripts>/route_engine.py" --role implementation --task-route <route> --task-id <task-id>
   The router is model-centric: each route lists ranked models from the capability map, and
   the task id enables the adaptive split, so healthy engines (claude, codex, openrouter)
   share standard and complex work in proportion to their headroom. `engine: codex` or
   `engine: openrouter` is a normal outcome, not only a fallback. Delegate it.
   Any script exiting 2 means invalid configuration: report the error and stop.
5. Use the selected Claude model and effort for normal work. Trivial tasks: do them
   directly, no subagents, no review fan-out.
6. Use the `scout` agent for repository exploration.
7. Use the `implementer` agent for standard implementation.
8. Use the `architect` agent for critical work, and for complex work if the design or root cause is unclear.
   `supercritical` work (frontier reasoning) goes to the top model, which only reasons, maps
   the task into sub-tasks (each at most `critical`) and reviews; it never implements. Before
   its first step ask the user for the effort and record it with
   `engine_state.py set-effort --task-id <id> --effort <e>`; route sub-tasks with
   `--parent-task-id <id> --permission-mode <mode>` from the hook context.
9. When executing a written plan, including Superpowers `executing-plans` or
   `subagent-driven-development`, treat each plan task as its own task: classify and
   route it with `route_engine.py --task-id <plan-task-id>`, implement with the
   orchestrator's `implementer` agent or Codex handoff as routed, review with
   `verifier`/`codex-reviewer` as routed, and apply the exit-42 fallback.
   Do not dispatch generic subagents with the session model for plan tasks;
   brainstorming and plan writing stay in the session.
10. Re-tier at most once per task, as in `references/routing-policy.md#re-tiering`: rerun
   `route_engine.py` with the scout's route or one step up/down from the implementer
   (never below the safety floor or down from critical or supercritical), plan if it became complex/critical, and hand off.
11. Run the configured test command. On failure, give the same engine one fix pass with
   only the failing output (via a handoff); if it still fails, hand it once to another
   engine (`--exclude-engine`, or one Claude tier up), then stop and ask.
12. For review, run `route_engine.py --role review --task-route <route> --implementation-model <model>`.
   The reviewer is chosen by capability, not engine: the cheapest model in the route's row at
   least as capable as the implementer, never the same model that implemented. Trivial
   tasks get no review.
   `verifier` on Claude; `codex-reviewer` on Codex or OpenRouter (its prompt names the
   wrapper, `codex_with_fallback.sh` or `openrouter_run.py`, and the model). Also run
   `--role style_review`: it covers standards, best-practice and reduction in parallel, given
   the lint output if configured, as `style-reviewer` on Claude or as `codex-reviewer`
   (asked for those categories only) on Codex or OpenRouter.
13. If an engine exits with code 42, mark it limited
    (`engine_state.py mark-limit --engine <engine> --message "<first line>"`), create a
    handoff with `write_handoff.py` listing completed and remaining work, and re-route the
    remaining work with `--exclude-engine <engine>` (`claude_with_fallback.sh`,
    `codex_with_fallback.sh` or `openrouter_run.py` with the handoff file). Retry once per
    engine per task. Exit 43 means an OpenRouter run paused over its daily/weekly cap
    (`OPENROUTER_PAUSED <run-id>`, details in
    `.claude/orchestrator/openrouter-runs/<run-id>/paused.json`): tell the user and ask
    continue or stop (`openrouter_run.py --resume <run-id>` / `--stop <run-id>`); never retry
    or fall back on 43 by yourself.
14. Never delegate authentication, permission, network, or ordinary tool errors as quota
    failures. Exit code 1 is an ordinary failure: report it.
15. If all engines are constrained, stop fan-out and require human approval.
16. After completion, run `engine_state.py record-result` for each engine used and save
    the final summary to `.claude/orchestrator/results/<task-id>.md`. When a nontrivial
    task passed its tests and review without needing a human, run `--role docs` last:
    `doc-updater` on Claude, or the engine's wrapper with `workspace-write` and the
    `doc-updater` rules in the prompt. It updates only AI instruction docs, when needed.

All delegated work follows `references/coding-standards.md`. The full routing table is
in `references/routing-policy.md`.
