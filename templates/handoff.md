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
- Do not commit, push, or open pull requests unless the objective says to.
- Never add agent attribution (`Co-Authored-By` trailers, "Generated with" lines) to commits or PRs.

## Output contract
Return:
1. Summary of changes
2. Files changed
3. Tests run and results
4. Remaining risks
5. Any work intentionally not completed

## Do not redo
{{completed_work}}
