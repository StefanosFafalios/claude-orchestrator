# Orchestration handoff

## Task ID
{{task_id}}

## Original objective
{{objective}}

## Task route
{{task_route}}

## Implementing engine
{{source_engine}}

## Replacement engine
{{target_engine}}

## Completed work
{{completed_work}}

## Remaining work
{{remaining_work}}

## Relevant files
{{relevant_files}}

## Current diff
```git
{{git_diff}}
```

## Test output
{{test_output}}

## Project commands
- Test: {{test_command}}
- Lint: {{lint_command}}
- Typecheck: {{typecheck_command}}

## Constraints
- Make the smallest viable change.
- Do not modify unrelated files.
- Do not redo completed work.
- Do not broaden scope.
- Do not store secrets or credentials.
- Follow the project's existing code style.
- Codex must read the project common instruction file (AGENTS.md points to it) and follow applicable nested AGENTS.md files before working.
- Do not commit, push, or open pull requests unless the objective says to.
- Never add agent attribution (`Co-Authored-By` trailers, "Generated with" lines) to commits or PRs.

## Output contract
Reply to the orchestrator, not a user: dense, no preamble, narration or restated task.
STATUS: done | partial | blocked | failed
RESULT: changes made; files changed
EVIDENCE: tests run and results; key values only
NEXT: work intentionally not completed and remaining risks, or none
End with an "Approach and decisions" section: at most 3 bullets on what was done, why, and alternatives rejected.

## Do not redo
{{completed_work}}
