---
name: review-models
description: Use when reviewing which OpenRouter/Claude/Codex models the plugin should route to, or when new models are released.
---

# Review models

Maintainer-only (lives in this repo's `.claude/skills/`, not shipped). Updates the capability
map in `templates/orchestrator.config.json`:

- `models`: canonical model id -> `{score, engines: {claude|codex|openrouter: name}}`
- `routing`: role (`scout`, `architecture`, `implementation`, `review`, `style_review`) ->
  route (`trivial`, `standard`, `complex`, `critical`, `supercritical`) ->
  `{models: [ranked ids], effort}`. Supercritical effort is `"ask"`. `implementation.supercritical`
  reuses the critical row: top models never implement.

Never update the map unattended; every change passes owner approval (step 4).

## 1. Catalog

```bash
python3 .claude/skills/review-models/catalog.py > /tmp/catalog.json   # or --input saved.json
```

Fetches the public `/models` list (`OPENROUTER_API_BASE` overrides the host) and keeps models
with tool calling, context >= 64k (`--min-context`), not `:free`/`:batch`/`~alias`, priced, and
not expired. Output is cheapest-first with `blended_usd_per_mtok = (3*prompt + completion)/4`.

## 2. Evidence

- Coding/agentic benchmarks only: SWE-bench Verified, Terminal-Bench, Artificial Analysis
  coding/intelligence index, or equivalents.
- Record every score with its source URL and date.
- OpenRouter usage rankings are popularity, not quality evidence. Ignore them.
- Fold the scores into one composite `score` 0-100 and write it to `models.<id>.score`.

## 3. Fit

- Set a minimum score bar per role x route.
- List every model clearing the bar. Order cheapest-first for implementation, scout and
  style_review; strongest-first for review and architecture.
- `supercritical` rows hold only the top models.
- The same canonical model on several engines is one `models` entry with several `engines`
  names (e.g. `claude` and `openrouter` slugs).
- Keep a comparable runner-up in review rows for the same-model safeguard.

## 4. Proposal

Present a table: role, route, current, proposed, score, blended price, reason. Stop and wait
for the owner's approval before touching any file.

## 5. Apply

1. Edit `templates/orchestrator.config.json`.
2. Bump `version` in `.claude-plugin/plugin.json`. (A seed done in the same release as another
   bump does not bump twice.)
3. Smoke-test each new OpenRouter slug:
   `OPENROUTER_MODEL=<slug> OPENROUTER_SANDBOX=read-only python3 scripts/openrouter_run.py <task-file>`, where the task file asks the model to list
   one file.
   This spends real money with the owner's key: get explicit owner approval first, and never
   print or store the key. Drop models that fail tool calls.
4. Append a dated entry to `.claude/skills/review-models/decision-log.md`: what changed,
   evidence links, and the spend of the smoke tests.
5. Run `python3 -m pytest -q && ruff check .`.
