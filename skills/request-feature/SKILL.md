---
name: request-feature
description: Use when the user wants orchestration, routing, review or workflow behavior that the cost-aware-orchestrator plugin does not provide, or asks to file a feature request or issue for the plugin. Checks the plugin's existing features first, then files a generic GitHub issue on the plugin's repository.
---

Files a feature request for the cost-aware-orchestrator plugin itself. It never
implements the feature in the consuming project.

## Steps

1. **Check it is really missing.** Look through the plugin's skills, commands,
   agents, hooks and workflow under `${CLAUDE_PLUGIN_ROOT}`. If the behavior
   exists, explain how to use it and stop.
2. **Resolve the repository.** Read `repository` from
   `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json` and reduce the URL to
   `owner/name`. Never hard-code it. If `gh` is missing or not authenticated
   (`gh auth status`), print the drafted issue and suggest `! gh auth login`
   instead of failing.
3. **Check for duplicates.**
   `gh issue list --repo <owner/name> --state open --search "<keywords>"`.
   If an issue already matches, create nothing; show its link and optionally
   offer a comment describing the new use case.
4. **Draft.** Title: imperative, 72 characters or fewer. Body sections:
   Problem (what the user tried to do, stated generically), Proposed behavior,
   Acceptance criteria, Notes (plugin `version` from plugin.json). Add the
   `enhancement` label only if it already exists; never create labels.
5. **Create.** Pipe the body through stdin, never interpolated into the shell:
   `gh issue create --repo <owner/name> --title "<title>" --body-file -`.
   The owner's standing instruction authorizes filing in this situation. Reply
   with the issue URL and a short title plus body summary.

## Privacy

The repository is public. The issue must be generic: no project names, paths,
file contents, code, URLs, company or customer names, credentials or secrets.
Describe the need abstractly, as `engine_state.redact` would leave it. Add
nothing about the user's identity beyond what `gh` attaches.
