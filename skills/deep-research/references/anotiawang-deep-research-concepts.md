# AnotiaWang/deep-research-web-ui — Concept Map

Two-sided comparison against `research/deep-research` as of the v1.9.0 vendor
pass. Upstream read at commits: `AnotiaWang/deep-research-web-ui` @ `bea05f5`
(2026-10-09), `dzhng/deep-research` @ `1f8f3e2` (2026-04-11). This file is the
source-of-truth map so later vendor sessions proceed from it instead of
re-deriving the comparison. SKILL.md carries only the pointers.

## Engine topology (both sides)

Upstream (Nuxt app, engine in `lib/core/` + `shared/utils/`): query →
`generateSearchQueries` (breadth queries, each a structured plan) → per-node
`processSearchResult` extraction (learnings + quotes + follow-ups + rewrite) →
deterministic `assessSearchLearnings` → recursive descent on follow-up questions
with `ceil(breadth/2)` width → `writeFinalReport`. dzhng's original (~500 LoC) is
the same loop without plans, quotes, reads, or rewrites.

Local skill (pure prompt loop): Clarification → Plan (sub-questions, complexity
caps) → rounds of gap-driven Query → Search → Extract → Synthesize → Stop →
gated report. No parallel nodes, no UI; the agent itself is the runner.

## Two-sided diff

### Upstream has, skill lacked → vendor candidates

| Concept | Upstream file | Status in this skill |
|---|---|---|
| Verbatim-quote evidence anchoring + deterministic containment match + one copy-exactly repair | `shared/utils/search-learning.ts`, `lib/core/extract-learnings.ts` | **Vendored v1.8.2** — `check-quote` subcommand, §3c subsection, page-text substrate in §3b.1 |
| Structured search plan, filters separate from query text, frozen constraints across retries/depth | `shared/utils/search-plan.ts` | **Vendored v1.9.0 (adapted)** — §3a "mini search plan" (intent maps to provider verticals; time/domain scope as verification lens, frozen on rewrite) |
| Query planning discipline: one angle per query, preserve identifiers/error strings, no invented domains, "publication metadata is a hint, not proof" | `shared/utils/search-plan.ts` (`searchPlanningRules`) | **Vendored v1.9.0** — §3a |
| Provider-specific query-syntax guidance | `shared/utils/search-plan.ts` (`searchQueryGuidance`) | **Vendored v1.9.0 (adapted)** — provider `intent` verticals play the provider-role |
| Presets {breadth, depth} + `estimateMaxSearches` budget announcement + cheapest-mode suggestion | `shared/utils/research-input.ts`, `lib/core/feedback.ts` | **Vendored v1.9.0 (adapted)** — complexity presets gain a per-round breadth; flat/tree budget via `estimate-budget`; plan announces "search budget: ≤N queries" |
| Breadth-halving recursion on follow-up questions; original goal threaded with every sub-query | `src/deep-research.ts` (dzhng), `lib/core/deep-research.ts` | **Vendored v1.9.0 (bounded)** — deep-dive chains: one per sub-question, width = ceil(breadth/2) per level, root goal re-anchored; NOT full tree machinery |
| Per-extraction follow-up questions feeding deeper search | `lib/core/extract-learnings.ts` | **Vendored v1.9.0** — per-query thread directions (≤2, only when a material gap remains) feed §3a round 2+ |
| On-demand full-page reading: request reads only when a concrete question exists; budgets per node/run | `lib/core/read-source.ts`, `readRequests` schema in `extract-learnings.ts` | **Vendored v1.9.0 (adapted)** — §3b snippet-first triage: snippets are first-class evidence; full-page fetch on demand with per-round ≤2 and run-wide budget |
| URL allow-list enforcement ("each url MUST be copied exactly from this allow-list") | `lib/core/extract-learnings.ts` | **Vendored v1.9.0** — folded into §3c anchoring: a stored claim's source_id must match the extract it anchors against |
| Deterministic search assessment reasons (verified/no_results/irrelevant/no_findings/unmatched_sources/unmatched_quotes) driving bounded retry | `shared/utils/search-assessment.ts` | **Vendored v1.9.1 (guidance-level)** — §3a round-outcome classification table; each reason maps to one bounded response. Full deterministic script port declined: relevance stays agent-judged here, so the counters would be agent-internal; `check-quote` already covers `unmatched_quotes` deterministically |
| Identical-rewrite ⇒ stop | `lib/core/deep-research.ts` (rewrite comparison) | **Vendored v1.9.0** — §3a refinement adds the normalized-identical check |
| Term-scored original-passage selection with omission markers under a prompt budget | `lib/core/source-context.ts` | **Vendored v1.9.0 (guidance-only)** — §3d; the mechanic largely exists in the tool tier (an MCP reader's `focus`/`section` modes) |
| Surgical report revision: citation→block map, bounded follow-up (1 round ≤2 directions), patch only cited blocks, all-or-nothing validation, sources appended, original kept in history | `shared/utils/report-revision.ts`, `lib/core/report.ts` (revision flow), README evidence panel | **Vendored v1.9.0** — Step 6 with `map-citations` subcommand; gate re-run required; history as `<name>-rev.md` files |

### Evaluated, NOT vendored (and why)

- **Full tree recursion with parallel nodes and per-node retry** — the skill's
  flat round loop keeps refutation polarity, gap-driven generation, the stopping
  check, and token budgeting in one controllable stream; full tree machinery
  would fork all of that while subagent mode already covers genuine parallelism.
- **Plain-prompt streaming-partial-JSON parsing / zod schema repair** — the
  runner is the model; structured-output robustness is upstream's provider-
  compatibility layer, meaningless here.
- **Tree visualization / SSE streaming / client-server mode / proxy config /
  provider key pools** — UI and deployment concerns, no prompt-loop analog.
- **dzhng's system prompt ("the source is irrelevant — value good arguments
  over authorities")** — directly contradicts this skill's provenance
  discipline; must never be adopted.

### Skill has that upstream lacks (do-not-regress list)

Four-label evidence basis `[VERIFIED]/[SOURCED]/[REASONED]/[ESTIMATED]`;
primary/secondary/tertiary source tiers with weighting and distribution checks;
mandatory refute-polarity queries; Contradictions + Gaps report sections with
deterministic structure checks; three validation gates with the
validate→fix→re-run loop; category-specific report formats; vault integration;
subagent mode; date-grounding discipline; clarification thresholds (upstream
always asks); stats block. Every vendored rule below extends these — none
replaces them.

## Adaptation notes (tooling deltas)

- Upstream search providers accept structural filters (timeRange, domains);
  the MCP search tier does not. Therefore intent maps to the `intent`
  verticals (web/news/paper/entity/code), and time/domain constraints act as a
  verification lens in §3c rather than enforced SERP filters.
- Upstream `readSource` is a tool-tier scrape; locally the same mechanic is
  an MCP reader (`web_fetch` with mode `scan`/`read`/`deep`, `focus`, `section`,
  `must_contain`) and built-in `web_extract`, so the budget is behavioral.
- Upstream evidence history lives in browser localStorage; locally a revision
  is a new `<name>-rev<N>.md` file next to the original, and the evidence store
  (run dir) continues accumulating.
- Upstream quote bounds (8–1500 chars) adopted verbatim in `check-quote`.