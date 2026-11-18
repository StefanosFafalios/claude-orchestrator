# Model review decision log

Append one dated entry per review run, newest last: what changed in the capability map, the
evidence links (with dates), and the spend of the smoke tests.

## 2026-11-18: first evidence-based seed

Approved by the owner (proposal: `.superpowers/sdd/2026-11-18-openrouter-engine/task-11-proposal.md`).

**Models.** Scores are now a composite: the mean, over the available benchmarks, of each
model's best result as a percentage of the best single-model result.

| model | score (was) | engines |
|---|---|---|
| `fable` | 98 (95) | claude, openrouter `anthropic/claude-fable-5.2` |
| `gpt-6.2-astra` | 97 (new) | codex, openrouter `openai/gpt-6.2-astra` |
| `opus` | 95 (85) | claude, openrouter `anthropic/claude-opus-5.5` |
| `gemini-4-carbon` | 93 (new) | openrouter `google/gemini-4-carbon` |
| `sonnet` | 92 (70) | claude, openrouter `anthropic/claude-sonnet-5.5` |
| `gpt-6.2-sol` | 90 (new) | codex, openrouter `openai/gpt-6.2-sol` |
| `gemini-4-hydrogen` | 84 (new) | openrouter `google/gemini-4-hydrogen` |
| `gpt-6.1-terra` | 83 (72) | codex, openrouter `openai/gpt-6.1-terra` |
| `haiku` | 58 (40) | claude, openrouter `anthropic/claude-haiku-5.5` |
| `deepseek-v4.1-flash` | 56 (new) | openrouter `deepseek/deepseek-v4.1-flash-1017` |
| `gpt-6-luna` | 36 (55) | codex, openrouter `openai/gpt-6-luna` |

Removed: `gpt-6-astra` and `gpt-6.1-sol` (beaten on price and score by GPT-6.2 Astra/Sol),
`gpt-5.6-terra` and `gpt-5.6-luna` (below the quality bar; no Codex-only tails).
Considered and not added: Kimi K3.1 Pro, Muse Ember, GLM-5.3, GLM-5.3-Flash, Qwen4 Max,
Gemini 4 Argon, Grok 4.7, Muse Wonder, MiMo-V2.6-Pro (each beaten on price and score).

**Rows.** Every row is ordered cheapest-first by blended OpenRouter price, ties by higher
score. The router keeps review quality (a reviewer scores at least the implementer and is
never the same model), so review and architecture rows no longer go strongest-first. Each
review row holds a stronger, different reviewer for every model its implementation row can
pick on a Claude-only or Codex-only setup; this put `gpt-6.2-astra` at the end of
`review.standard`. The exception is Opus on Claude alone: only `fable` outranks it, and top
models stay in supercritical rows (effort asked), so that review falls back to the next-best
different model with human review.
Architecture for trivial/standard moved from Opus to Sonnet. Trivial tasks get no model
review, so `review.trivial`, `style_review.trivial` and `scout.trivial` were removed (nothing
routes to them).

**Owner calls.** Keep the Claude aliases `haiku`/`sonnet`/`opus` and re-run this review on
each Claude release (the OpenRouter slugs are pinned to 5.5). Gemini 4 Carbon uses
OpenRouter's live price ($2/$10, $4 blended), not Artificial Analysis's $4/$20. Trivial,
scout and style rows may go wholly to DeepSeek when OpenRouter is enabled; budget pacing
caps it.

**Evidence** (all retrieved 2026-11-18):

- Artificial Analysis Intelligence Index, Terminal-Bench 4.0, FrontierCode 2.0:
  https://artificialanalysis.ai/leaderboards/models; for models no longer listed as current,
  https://artificialanalysis.ai/models/gpt-6-astra, https://artificialanalysis.ai/models/gpt-6-1-sol,
  https://artificialanalysis.ai/models/gpt-5-6-terra, https://artificialanalysis.ai/models/gpt-5-6-luna
- Artificial Analysis Coding Agent Index v2.1: https://artificialanalysis.ai/agents/coding
  (mirror: https://benchlm.ai/benchmarks/aacodingagents)
- Terminal-Bench 4.1: https://benchlm.ai/benchmarks/terminalBench2 (snapshot 2026-11-17;
  the multi-model "Copilot Fusion" entry is excluded)
- SWE-bench Verified looked at, not used (no rows for current models, all self-reported,
  saturated): https://benchlm.ai/benchmarks/swe-bench-verified
- Prices and slugs: `GET https://openrouter.ai/api/v1/models` through `catalog.py`
  (278 models kept)

**Smoke tests: pending owner key** for all 11 OpenRouter slugs above. Spend so far: $0.
Codex names `gpt-6.1-terra`, `gpt-6.2-sol` and `gpt-6.2-astra` are not yet in this
machine's Codex model cache; `engine_name` skips them until they appear.
