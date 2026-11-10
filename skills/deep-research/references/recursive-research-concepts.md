# Anjos2/recursive-research — Concept Map

Light vendor pass at v1.9.2. Upstream: `Anjos2/recursive-research` @ `15ed326`
(v2.2.0, 2026-04-22 — last commit; MIT). Single-SKILL.md Claude Code plugin,
Spanish-language, prompt-only. Reviewed and rejected as an installed skill
(stale, redundant with this skill's discipline); only three concepts ported.

## Vendored (guidance-level, adapted)

| Concept | Upstream location | Landing |
|---|---|---|
| Saturation stop signal: new material findings this cycle ≤5% of accumulated total, held across consecutive cycles → close | SKILL.md §5.7 PhD fitness criterion 3 | §3f Stop-if: ≤5% for 2 consecutive rounds. Threshold reduced from 3 cycles to 2 for round-loop scale and flagged as unvalidated upstream — a strong signal, never a substitute for the qualitative stop checks |
| Coverage balancing: pick the least-covered thread each cycle | SKILL.md §5.1 | §3a round 2+ rule: any planned sub-question at zero coverage gets a query before the loudest gap is followed; mandatory before optional deep-dive queries |
| Munger-inversion "what bias do my sources share?" at close | SKILL.md §5.2 + Fase 6 gaps.md | Step 4 Gaps section: mandatory first-gap line naming the shared bias (language, era, school, jurisdiction, industry); a homogeneous source set is itself a gap |

## Evaluated, not vendored

- **Full cycle machinery** (20-cycle caps, ciclo-N.md checkpoints, estado/hilos
  provenance files, resume modes) — this skill's run store (claims.jsonl,
  pages/, append-only evidence) already covers persistence with stronger
  fabrication-detection properties; a second file protocol would fork them.
- **WDM weighted-decision-matrix scoring** for thread and source selection —
  formal scoring ceremony with no evidence of benefit; the round loop keeps
  relevance agent-judged (see the AnotiaWang map for the same rejection of
  deterministic score tables).
- **Per-domain pre-loaded seed source lists** — generic and partly dated;
  §3c's tier filter and the vault cover this better.
- **Preventive context-pause thresholds** (tool_calls ≥150 / output ≥80k) —
  Hermes compaction plus the claims.jsonl re-read contract handle this; asking
  the user to pause does not fit autonomous runs.
- **Local-corpus mode / Firecrawl-Context7 MCP detection** — harness-specific
  tooling; local reading here is plain file tools in the §3b budget.

## Do-not-introduce (upstream behaviors rejected on principle)

- The "PhD fitness function" as a **hard gate**: five measured criteria with
  auto-confirm prohibition sound rigorous but the counts (≥80% coverage,
  ≥3 Tier-1 per thread, ≥3 cross-connections) are model-assessed proxies —
  a red flag here is the same fabrication risk the validation gates exist to
  catch. Coverage/bias signals stay advisory in this skill.