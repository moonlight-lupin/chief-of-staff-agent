# Validation Gates — Deterministic Report Verification

This reference holds the full contract for `scripts/research_validation.py`:
the evidence store, the three gates, and the failure loop. SKILL.md carries
only the pointers; this file is the source of truth.

The script is stdlib-only and deterministic. Like the rest of this skill it is
MIT-licensed (see the plugin's NOTICE; the plugin itself is Apache-2.0). It checks
structure, not judgment. Concepts were ported from external skill reviews;
the implementation is original.

## Contents

- When to run
- Loop
- Evidence store (Step 3b.1)
- Quote anchoring (`check-quote`, v1.8.2)
- Known scoring limitations
- Tests

## When to run

Every report with 5+ sources passes all three gates before delivery.

```bash
python3 <skill_dir>/scripts/research_validation.py validate-report  --report <report.md>
python3 <skill_dir>/scripts/research_validation.py verify-citations --report <report.md>
python3 <skill_dir>/scripts/research_validation.py verify-claims    --dir <run_dir> --strict --provider-used mcp
```

**Optional advisory pre-screen (`--jev-prescreen`, v1.8.1).** On instances with a
TypeSafe Jev trial (`TYPESAFE_KEY`, prescreen module location set via the
`JEV_PRESCREEN_PATH` environment variable), `verify-claims --jev-prescreen`
first runs a semantic pre-screen: one System One choice per claim
(supported / partial / unsupported), compared against the
deterministic scorer. Confident disagreements print as `JEV-PRESCREEN advisory`
lines (stderr) plus a summary JSON — false-red candidates (gate capped a
semantically-supported claim) and false-green candidates (tokens matched but
snippet does not support). Contract:

- **Advisory only.** Never blocks, never changes the exit code; deterministic
  verdicts below the advisory lines are the source of truth.
- **Fails gracefully.** No `TYPESAFE_KEY`, absent prescreen module, network
  error, or API failure → `{"jev_prescreen": "SKIPPED", "reason": ...}` and the
  gate continues unchanged. The script stays fully offline-capable.
- Cost is small (measured: ~$0.0001 per 7-claim store, ~300 ms) and recorded in
  the summary line.
- When this instance has no Jev setup, omit the flag entirely — plain
  `verify-claims` behavior is unchanged.

- `validate-report`: required sections as `##` headings that start with the
  section name (`## Gaps and open questions` counts; `## Mind the Gaps` and
  `### Sources of revenue` do not) — including **Contradictions** and **Gaps**,
  both mandatory — no placeholder text (TBD/TODO/truncation), research-stats
  block present, evidence key legend under Sources (a missing Sources section
  no longer skips the legend check).
- `verify-citations`: every inline `[N]` resolves to a Sources row, every row
  is cited, URLs well-formed, suspicious generic-title patterns flagged, no
  citation ranges. A bracketed year (`fiscal [2024]`) is prose, not a citation,
  unless the Sources list has an entry with that number.
- `verify-claims --strict`: every factual claim's stored evidence snippet must
  actually support it (deterministic token/number/year/entity overlap; a
  contradicting figure or year caps the score below supported). Exit 1 on
  unsupported factual claims — fix the claim or add real evidence, never the
  score. `--provider-used <provider>` records the search provider actually
  used (e.g. `mcp`) into `run_manifest.json`; pass it on the final
  verify-claims run. Three further checks, warnings by default and failures
  under `--strict`:
  - **`[VERIFIED]` independence** — a claim stored with `"basis": "verified"`
    needs corroboration from sources on ≥2 different hosts (`www.` ignored), and
    each host counts only if its **own snippet** supports the claim (score
    ≥ 0.60 on its own). Because scoring is word overlap, a source that says the
    same thing in other words (a paraphrase: "uptake rose by 40 percent" for
    "adoption grew 40%") scores low and will not count — phrase the claim close
    to the sources' wording, or accept `[SOURCED]`.
  - **Counter-evidence** — once the store holds 5+ sources, at least one claim
    must have `"polarity": "refute"` **and** be evidence-backed: a snippet from
    a registered source that at least partially supports it. An empty refute
    claim does not count. If you searched and found none, pass
    `--refute-none "<what you searched>"`; the reason is kept in the manifest,
    and a blank reason is ignored.
  - **Source-quality mix** — reported as `source_quality` (counts plus
    healthy / acceptable / weak, per SKILL.md §3e). `weak` adds a warning but
    never fails: flag it under Gaps. Sources stored before quality was
    validated, with a value outside the three tiers, count as `secondary`. Sources stored before quality was
    validated, with a value outside the three tiers, count as `secondary`.

## Loop

Validate → fix → re-run all three. Max 3 cycles. Still failing after 3 → stop
and report the remaining problems to the user honestly. Never skip the gates,
never deliver with a red gate.

For quick 2-3 source reports (no evidence store), `verify-claims` returns a
warning rather than failing — record it in the stats block.

## Evidence store (Step 3b.1)

From the first retrieval round, persist evidence to disk so it survives
context compaction. Use the store subcommands:

```bash
python3 <skill_dir>/scripts/research_validation.py init-run --dir <run_dir> --query "<question>" --mode simple|moderate|complex --provider auto|<provider-name>
python3 <skill_dir>/scripts/research_validation.py register-source --dir <run_dir> --json '{"url": "...", "title": "...", "quality": "primary"}'
python3 <skill_dir>/scripts/research_validation.py add-claim --dir <run_dir> --json '{"claim_id": "c1", "claim": "...", "kind": "factual|interpretive|projective|synthesis", "polarity": "support|refute|neutral", "basis": "verified|sourced|reasoned|estimated", "snippet": "exact quote", "source_id": "<from register-source>"}'
python3 <skill_dir>/scripts/research_validation.py add-evidence --dir <run_dir> --json '{"claim_id": "c1", "snippet": "second corroborating quote", "source_id": "..."}'
```

- `quality` must be `primary`, `secondary` or `tertiary`; `polarity` and
  `basis` are validated too (case-insensitive). A typo is rejected, not stored.

- `run_dir` is `<project_root>/research/<YYYY-MM-DD>-<topic-slug>/`, the same
  folder as the final `report.md`, so the run is synced with the user's data.
- `register-source` returns a stable sha256 `source_id`; dedup is automatic
  (URL canonicalization strips tracking params).
- Add claims and evidence **during** the loop (§3d), not at the end. An
  append-only `claims.jsonl` is the fabrication-detection backbone for the
  gates.
- On context compaction, the store is ground truth — re-read `claims.jsonl`
  instead of trusting compressed memory.
- **Save each fetched page's text** to `<run_dir>/pages/<source_id>.txt` right
  after `register-source` (for pages `web_extract` stored to disk, its reported
  on-disk path can be passed to `check-quote --file` directly). This page-text
  file is the substrate for quote anchoring — see next section.

## Quote anchoring (`check-quote`, v1.8.2)

Extraction-time fabrication guard, ported from
[AnotiaWang/deep-research-web-ui](https://github.com/AnotiaWang/deep-research-web-ui)'s
`shared/utils/search-learning.ts`: a model-proposed excerpt becomes evidence
only after literal containment matching against the retrieved text. Every
snippet stored via `add-claim` / `add-evidence` must be a **verbatim contiguous
excerpt** of the fetched page, anchored before storing:

```bash
python3 <skill_dir>/scripts/research_validation.py check-quote \
  --dir <run_dir> --source-id <sid> --snippet "<copied excerpt>" [--snippet "..."]
python3 <skill_dir>/scripts/research_validation.py check-quote \
  --file <path-to-page-text> --snippet "<copied excerpt>"
```

- Resolution: `--file` anchors against any page-text file; `--dir --source-id`
  resolves `<run_dir>/pages/<source_id>.txt` and verifies the source_id exists
  in `sources.jsonl` first. Errors (unknown source_id, missing page file,
  unreadable input) exit 2.
- Matching is **exact modulo Unicode whitespace runs** (`\s+` → one space on
  both sides — no tokenizer). CJK and any other script anchor reliably, unlike
  the tokenized `support_score` scorer.
- Per-snippet output: `snippet_ok`, `chars`, and the 1-indexed `line` of the
  first match in the page file (re-read around it for context on repair).
- advisory `warning` for under-8-char snippets (too short to anchor) and
  over-1500-char snippets (usually a whole paragraph, not the supporting run).
  Warnings never change the exit code.
- A failed match that *would* match after normalizing curly quotes / dashes /
  figure-width spaces to ASCII emits `hint: quote-style mismatch` — re-copy the
  excerpt with the page's original punctuation. Still a non-zero exit.
- Exit codes: 0 all snippets matched · 1 at least one failed · 2 usage error.
- **Repair-once rule (Step 3c):** on a mismatch, re-copy the excerpt exactly
  from the page text once. Never massage the snippet toward the claim, never
  splice across paragraphs, never add ellipses. If no contiguous supporting
  excerpt exists, the claim is not evidence-backed: store it honestly
  (`[REASONED]` / interpretive without a quote, or accept the red gate flag)
  rather than smoothing it through. An unanchored snippet is itself a gate
  defect — `verify-claims` scores whatever is stored and cannot detect a
  well-paraphrased quote.

## Support commands (v1.9.0)

Not gates — helpers the SKILL.md workflow invokes. Both stdlib-only, offline.

- `estimate-budget --breadth B --rounds R [--halving]` — upper bound of planned
  queries, adapted from upstream `research-input.ts` `estimateMaxSearches()`.
  Flat mode = breadth × rounds (the round loop). Tree mode (`--halving`) =
  upstream's breadth-halving recursion, the upper bound when §3a deep-dive
  chains are active. Breadth/rounds clamped to 1-8 (exit 2 outside). Test
  vectors: flat (2,1)=2, (3,3)=9; tree (2,1)=2, (2,2)=4, (3,3)=15, (4,3)=20,
  (4,5)=36.
- `map-citations --report R.md [--citation N]` — maps each inline `[N]` citation
  to the top-level markdown blocks citing it, adapted from upstream
  `report-revision.ts` `findCitedReportBlocks()`. Fenced code and HTML/reference-
  definition blocks are skipped (citation ranges inside code never map); tables
  and lists stay whole blocks. Output carries `start_line`/`end_line` per block
  (1-indexed, inclusive) for targeted edits, `sources_count` parsed from the
  `## Sources` section, and per-citation block ids. Exits 0 ok · 1 when
  `--citation` is given but no block cites it · 2 file problems. Used by
  SKILL.md Step 6 (follow-up report revision); the three gates above must pass
  on the revised report before delivery.

## Known scoring limitations

The support scorer is deterministic token/number/year/entity overlap with
contradiction caps. Accepted limitations, by design:

- Negation and polarity are not parsed.
- Entity overlap can false-red on capitalized common nouns.
- **Claim shape law (validated live): a claim scores "supported" only when ONE
  of its stored snippets covers every figure, year and entity the claim text
  carries.** Anything the claim names that its snippets lack caps the score at
  PARTIAL−0.01 — a claim carrying three facts costs three snippets' coverage,
  so split multi-fact claims or byte-align the claim to its quote. The
  near-byte-identical claim is the only stable store form; prose paraphrase
  stays in the report body.
- **Entity matching is exact-set, not substring, and multi-word capitalized
  runs glue into one token**: the page phrase "Scrape URL FIRECRAWL_SCRAPE"
  forms the entity `scrape url`, so a claim saying just "Firecrawl" does NOT
  set-match it (and vice versa). Do not glue capitalized words in a claim when
  the snippet separates them.
- **Alphanumeric brand prefixes tokenize as figures**: "x402" yields the
  figure `402` and "v2.1.288" yields `2.1.288` — a claim containing such a
  prefix caps unless its snippet contains the same brand text.
- A missing signal in the snippet (no figures, no years) caps the score
  rather than proving support.
- Magnitude abbreviations are normalised (`2.4bn` = `2.4b` = `2.4 billion`;
  `mn`/`mln` = `m`; `tn`/`trn` = `t`), but other unit variants still count
  as different figures — write figures in one format per report. `mm` is
  deliberately **not** read as million: it is also millimetre, so `2mm` vs
  `2m` stays a contradiction.
- A yearlike value adjacent to a magnitude word is treated as prose.
- Digit runs inside product names ("iPhone15", "v2000") are figures, not years.

## Store-loop hygiene (v1.9.1)

When batching store subcommands inside one script loop, check EVERY response's
JSON before printing progress — a rejected call (invalid `basis` or `kind`,
duplicate claim_id) exits 2 silently while the loop's own "stored" message
lies. `basis` takes ONLY the four labels (`verified|sourced|reasoned|
estimated`); a source TIER name (`primary`/`secondary`) is not a basis value —
the tier lives on the `register-source` record, the basis label on the claim.
Diagnostics: import the script's helpers and print `_figures`/`_entities`/
`_tokens` sets per claim-vs-snippet to see which axis capped the score.

**Wrong-substrate anchors produce false reds**: `check-quote` reads
`pages/<source_id>.txt` — verify the source_id resolves to the URL the quoted
sentence actually came from before treating a mismatch as a fabrication signal
(the same sentence lives on several pages; quoting against the wrong page
file reads as unmatched even though the quote is real).

## Tests

`tests/test_deep_research_gates.py` (plugin root) covers the evidence store,
claim-support scoring, citation verification, and structure validation;
`tests/test_deep_research_v072.py` covers the v0.7.2 hardening and
`tests/test_deep_research_v074.py` pins the PR #27 fixes. Run with
`python3 -m pytest tests/test_deep_research_gates.py tests/test_deep_research_v072.py
tests/test_deep_research_v074.py`.
Run with `python3 -m pytest research/deep-research/tests/ -q`.