# Claude Code Instructions

Before working on this repository, read:

- `docs/AI_CONTEXT.md`

That document is the canonical source of shared project knowledge and conventions.
Follow its [coding standards](docs/AI_CONTEXT.md#coding-standards) and
[commit policy](docs/AI_CONTEXT.md#commits-and-pull-requests) for every change.

## Claude Code-specific behavior

This repository *is* a Claude Code plugin. To try local changes, run
`claude --plugin-dir .` from a scratch project rather than installing from the
marketplace. Plugin layout rules (what lives in `.claude-plugin/`, how
`${CLAUDE_PLUGIN_ROOT}` is used, workflow-script limits) are in the canonical document.
Keep Claude-specific instructions here and shared guidance in the canonical document.
