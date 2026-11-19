# Web research role — design

Issue: #3. Owner-approved 2026-11-19 (OpenRouter paid search allowed; deep research automatic on critical and above).

## Intent

Scout only reads the local repository. Some tasks need outside knowledge first: current state of the art, library or
API changes after a model's training cutoff, or a short literature review. Add a research step that searches the web
and hands a cited report to planning and implementation, only when needed.

## Goals, non-goals, anti-goals

- Goals: a `research` routing role; runs between scouting and planning only when flagged; subscriptions first, then
  OpenRouter search models; cited Markdown report; spend visible in the OpenRouter budget.
- Non-goals: research on trivial tasks; caching reports across tasks; research-specific capability scores in the
  reviewer rule.
- Anti-goals: project code, file contents, paths, private names or secrets leaving the machine in queries; research
  running when not flagged; a failed research step blocking the task.

## Approaches

1. **New `research` role (picked).** Claude agent with web search/fetch, Codex with `--search`, and a direct OpenRouter
   call for Perplexity Sonar models (they search natively and cannot drive tools, so they cannot run through the
   Codex-based `openrouter_run.py`).
2. Extend scout with web tools: mixes local and web work and scout runs on the cheapest models; rejected.
3. Standalone `/research` command only: never automatic; rejected.

## Design

- **Catalog:** add `sonar`, `sonar-reasoning-pro`, `sonar-deep-research` to `models` (OpenRouter names
  `perplexity/sonar`, `perplexity/sonar-reasoning-pro`, `perplexity/sonar-deep-research`). Their `score` is not a coding
  score; they appear only in research rows.
- **Rows (`routing.research`):** trivial unused; standard `haiku`, `gpt-6.1-terra`, `sonar` (low); complex `sonnet`,
  `gpt-6.2-sol`, `sonar-reasoning-pro` (medium); critical and supercritical `opus`, `gpt-6.2-astra`,
  `sonar-deep-research` (high). Row order puts subscriptions first; `fable` never appears. Configs without
  `routing.research` load and never research.
- **Trigger:** the decider returns `needs_research: true|false` (missing → false) in the same call; without the decider
  the session sets it; the architect may request research in its plan output. The workflow runs a `Research` phase
  after Scout and before Plan only when flagged and the tier is not trivial.
- **Engines:**
  - Claude: new `agents/researcher.md` (tools: WebSearch, WebFetch, Read) at the routed model/effort.
  - Codex: `codex_with_fallback.sh` honors `CODEX_SEARCH=1` → `--search`; read-only sandbox.
  - OpenRouter: new `scripts/research_run.py --query-file F`: one chat-completions call to the routed Perplexity slug
    (key only in the Authorization header), prints the report with sources and the call's cost when reported. Before
    calling, refuses when OpenRouter is not enabled, constrained, or a budget cap is spent (cached snapshot, like the
    decider). Exit 0 report; 42 only for a usage limit (402 / key limit); 1 other failures.
- **Query privacy:** the session (or workflow) writes a generic question; `research_run.py` and the agent prompts pass
  it through `engine_state.redact`, cap it at 2000 characters, and the instructions forbid code, paths and names.
- **Output:** Markdown report (findings, sources with URLs and dates) saved in the run directory and given to the
  architect and implementer prompts. A failed or skipped research step logs and continues.
- **Hook:** the `needs_research` instruction and the research row appear only when the config has `routing.research`.
- **Model review skill:** research rows come from search/research benchmarks, cheapest-sufficient first, subscriptions
  before paid search, never `fable`.

## Left to the builder

Exact prompt wording, report file name, helper names, test layout.
