# Model review decision log

Append one dated entry per review run, newest last: what changed in the capability map, the
evidence links (with dates), and the spend of the smoke tests.

## 2026-11-18: first evidence-based seed

Approved by the owner on 2026-11-18. The proposal itself was a local working file; its
evidence is reproduced below.

**Models.** Scores are now a composite: the mean, over the available benchmarks, of each
model's best result as a percentage of the best single-model result. Each model is taken at
its best (usually max-effort) configuration. The best single-model results used as 100:

- AA Intelligence Index: 64.56
- AA Coding Agent Index v2.1: 58.5
- Terminal-Bench 4.1: 65.76 (the multi-model "Copilot Fusion" at 71.21 is excluded)
- AA Terminal-Bench 4.0: 67.2
- AA FrontierCode 2.0: 49.1

Raw results ("–" = no published result):

| model | AA Intelligence Index | AA Coding Agent Index v2.1 | Terminal-Bench 4.1 | AA Terminal-Bench 4.0 | AA FrontierCode 2.0 | composite |
|---|---|---|---|---|---|---|
| Claude Fable 5.2 | 64.6 | 58.5 | 65.76 | 61.6 | 49.1 | 98 |
| GPT-6.2 Astra | 62.1 | – | – | 64.1 | 49.0 | 97 (3 sources) |
| Claude Opus 5.5 | 59.1 | 58.2 | 65.76 | 59.6 | 46.4 | 95 |
| Gemini 4 Carbon | 60.0 | 55.5 | 64.55 | 65.2 | 40.6 | 93 |
| Claude Sonnet 5.5 | 54.5 | 56.3 | 63.94 | 63.6 | 41.7 | 92 |
| GPT-6.2 Sol | 57.3 | – | – | 60.1 | 44.4 | 90 (3 sources) |
| GPT-6 Astra (removed) | 56.9 | 53.1 | 60.30 | – | – | 90 |
| GPT-6.1 Sol (removed) | 54.6 | 52.2 | 60.91 | – | – | 89 |
| Gemini 4 Hydrogen | 53.9 | 52.3 | 56.36 | 54.0 | 39.0 | 84 |
| GPT-6.1 Terra | 51.3 | 48.6 | 53.33 | 55.6 | 42.3 | 83 |
| Kimi K3.1 Pro (not added) | 46.5 | 48.6 | 59.70 | 67.2 | 25.5 | 80 |
| Claude Haiku 5.5 | 36.5 | 29.6 | 40.00 | 32.8 | 34.9 | 58 |
| Muse Ember (not added) | 36.0 | 39.7 | 30.61 | 40.9 | 27.2 | 57 |
| GLM-5.3 (not added) | 31.7 | 40.1 | 41.21 | 41.9 | 21.6 | 57 |
| DeepSeek V4.1 Flash 1017 | 32.6 | 36.8 | 38.48 | 33.8 | 28.5 | 56 |
| GLM-5.3-Flash (not added) | 27.8 | – | 36.97 | 32.8 | 17.6 | 46 |
| GPT-5.6 Terra (removed) | 32.3 | – | 21.52 | – | – | 41 (2 sources) |
| GPT-6 Luna | 27.9 | 25.9 | 15.15 | 12.6 | 25.2 | 36 |
| GPT-5.6 Luna (removed) | 23.8 | – | 17.58 | – | – | 32 (2 sources) |

Blended OpenRouter prices, (3 × prompt + completion) / 4 in $/Mtok, live catalog 2026-11-18:
fable 16, gpt-6.2-astra 16, opus 8, gemini-4-carbon 4, sonnet 4, gpt-6.2-sol 4,
gemini-4-hydrogen 3, gpt-6.1-terra 2, haiku 0.20, gpt-6-luna 0.20, deepseek-v4.1-flash 0.15.

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

**Smoke tests (2026-11-19, owner-approved, on demand):** all 11 OpenRouter slugs passed. Each ran
one read-only task through `openrouter_run.py` (Codex, effort low): list `scripts/` with a shell
tool call and report the `.py` count. Every run exited 0 in 5-15 s and used 11k-56k tokens; none
dropped. Counts were not graded (fable and opus answered 13, sonnet 10, the rest 12 of 12): the
test checks tool calling, not care at low effort. Spend: the key's `/key` counter still read $0
right after the runs (it lags); about 314k tokens in total.
Codex names `gpt-6.1-terra`, `gpt-6.2-sol` and `gpt-6.2-astra` are not yet in this
machine's Codex model cache; `engine_name` skips them until they appear.
