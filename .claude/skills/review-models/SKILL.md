---
name: review-models
description: Use when reviewing which OpenRouter/Claude/Codex models the plugin should route to, or when new models are released.
---

# Review models

Maintainer-only (lives in this repo's `.claude/skills/`, not shipped). Updates the capability
map in `templates/orchestrator.config.json`:

- `models`: canonical model id -> `{score, engines: {claude|codex|openrouter: name}}`
- `routing`: role (`scout`, `architecture`, `implementation`, `review`, `style_review`, `docs`, `decider`) ->
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
- List every model clearing the bar. Order every row cheapest-first by blended OpenRouter
  price; on a tie, the higher score goes first. Review rows can stay cheapest-first because
  the router keeps quality: it takes the first reviewer that scores at least the
  implementer's model and is not that model.
- Coverage: for every model an implementation row can pick on a Claude-only or a
  Codex-only setup, the review row for that route must hold a different model on the same
  engine scoring at least as high (the shipped-template test in `tests/test_route_engine.py`
  checks this). Strong models appended at the end of a review row cost nothing until needed.
  Exception: when only a top model outranks the implementer (Opus on Claude alone), leave it
  out; the router then picks the next-best different model and asks for human review.
- Never add a model below the quality bar just to give an engine work (no Codex-only tails):
  unavailable Codex names are skipped automatically.
- `supercritical` rows hold only the top models, and top models (e.g. `fable`) appear in no
  other row: their effort is always asked from the user because they are expensive.
- `docs` (the end-of-task doc updater) follows the `style_review` rule: the same models and
  effort, cheapest-first, `low`. A config without `routing.docs` routes on `style_review`.
- `decider` (the cheap tier classifier) has a single `standard` row: the cheapest models
  that reliably return tool-free JSON, score bar about the trivial implementation bar,
  cheapest-first, effort `low`. Only its OpenRouter names are used; a config without
  `routing.decider` has no decider.
- Trivial tasks get no scout and no model review: there are no `review`, `style_review`,
  `scout` or `docs` rows for `trivial` (`model_map.UNUSED`).
- The same canonical model on several engines is one `models` entry with several `engines`
  names (e.g. `claude` and `openrouter` slugs).
- Claude aliases (`haiku`, `sonnet`, `opus`) move with each Claude release while OpenRouter
  slugs are pinned: re-run this review on every Claude release.

## 4. Proposal

Present a table: role, route, current, proposed, score, blended price, reason. Stop and wait
for the owner's approval before touching any file.

## 5. Apply

1. Edit `templates/orchestrator.config.json`, including `map_meta.reviewed` (today's date).
2. Bump `version` in `.claude-plugin/plugin.json`. (A seed done in the same release as another
   bump does not bump twice.)
3. Smoke-test each new OpenRouter slug:
   `OPENROUTER_MODEL=<slug> OPENROUTER_SANDBOX=read-only python3 scripts/openrouter_run.py <task-file>`, where the task file asks the model to list
   one file.
   This spends real money with the owner's key: get explicit owner approval first, and never
   print or store the key. Drop models that fail tool calls. Test only new or changed slugs,
   and never `fable` (the top-priced model): its provider's next model (`opus`) covers the
   same tool-call path. The 2026-11-19 run of all 11 slugs cost $1.19.
4. Append a dated entry to `.claude/skills/review-models/decision-log.md`: what changed,
   evidence links, and the spend of the smoke tests.
5. Run `python3 -m pytest -q && ruff check .`.
