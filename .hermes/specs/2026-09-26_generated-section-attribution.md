# Generated-Section Attribution Spec (v3 — build-ready, post review round 2)

**Goal:** Stop CoS's regenerated markdown artifacts from clobbering operator annotations. One persisted, regenerated markdown artifact — the briefing archive — gets machine-managed generated sections with explicit boundaries. Regeneration replaces only content inside those boundaries. Operator-authored text anywhere else in the file survives every regeneration.

**Concept source:** Lumen's attribution-span contract (7 review rounds: R6 regressions fixed by R7-1 trailing-note preservation and R7-3 span splitting). Ported standalone; no lumen code dependency.

**Revision note:** v3 folds round-2 findings (Opus 3 BLOCKING/7 MAJOR/2 MINOR/1 NIT; Codex 4 BLOCKING/6 MAJOR/1 MINOR; both REQUEST CHANGES, convergent). Structural decisions: (1) minimal-slice deferrals baked in — retirement, the `generated-meta` line, the 20-file cap, the standalone lint command, `--output` merging, and the `cmd_run` archive hook are CUT from the build scope (both reviewers' cut lists); (2) recovery replaced by refusal — ambiguous markers (duplicate/nested/crossed/stray-end/unclosed fence) cause the merge to REFUSE the file, not repair it, closing round-2 B1/B2 growth loops by construction; (3) one shared registry `BRIEFING_ARCHIVE_SECTIONS` published with both producer mappings (round-2 B3); (4) hash contract made executable: LF-normalized bytes, trailing-whitespace-stripped, pinned byte range (round-2 M4/Codex #2); conflict-wrap grammar defined (`cos:conflict` fence lines that cannot match the C-1 marker regex); (5) validator split: shared scalar validator keeps newline collapse only; a merge-time body validator (inside the module) neutralizes `<!--`, `-->`, and fence openers line-by-line while preserving structure — `&lt;` escaping alone never neutralizes `-->` (Codex #5); (6) concurrency guarantee stated honestly: `fcntl` lock covers cooperating writers only; non-cooperating editors get best-effort detection, not a closed guarantee (Codex B4); (7) `--output` stays a plain overwrite; only the archive merges (Opus M7); (8) envelope schema, exit codes, and staging directory pinned; envelope PII never in `/tmp` (Opus M9); (9) doctor staleness signal institutionalizes live-adoption detection (the v0.5.x lesson); (10) surface inventory rationale corrected (workflow overlay rewrites unconditionally — explicit scope exclusion; named `--output` already regenerates into a fixed path).

## Background

CoS regenerates operator-facing markdown. If the operator annotates one of these files — a note next to an AR figure, a correction on a pipeline row — the next regeneration overwrites the whole file and the annotation is silently lost. Lumen solved the identical defect class for wiki pages. The porting rules carry the edge-case rulings:

- A generated span ends AT its closing marker line (R7-1: content after the marker survives).
- Marker-less paragraphs between two generated blocks form their own spans and are not absorbed (R7-3).
- Backward prefix discovery is NOT ported: text-matching cannot establish provenance, and an operator heading identical to a generated heading would be absorbed. Regression fixture required: an operator heading byte-identical to a generated heading survives untouched.

**Live-path note (v0.5.x lesson):** the daily briefing is agent-composed — the cron session renders prose via ad-hoc runner scripts, never calling `chief_of_staff.py daily`. The PRIMARY integration point is therefore the deterministic helper invoked by the composing session. The Python CLI wiring is secondary (slice 2). Both must change or the feature accrues zero live data.

## Surface inventory

| Surface | Path | Writer | Format | Stable across runs? | In scope? |
|---|---|---|---|---|---|
| **Briefing archive (primary)** | `project_root/briefing.md` | attribution helper (`merge --artifact briefing`) via JSON envelope | markdown | Yes (fixed name) | **YES — the only in-scope surface** |
| Daily briefing named output | `--output <path>` | `daily_briefing.py:_emit_rendered` (`dest.write_text`, plain overwrite) | text / markdown / html | Yes (operator-named) | No — stays plain overwrite (round-2 M7: downstream consumers must never see markers; no C-7 legacy inflation) |
| Daily briefing HTML attachment | `daily-briefing-{YYYY-MM-DD}.html` | `_emit_rendered` | html | No (date-stamped) | No |
| Weekly review | stdout only | `weekly_summary.py` | text | n/a | No |
| Workflow run pointers | session context | `workflow_hooks.pointer_strip` | text | n/a | No |
| `skills.local` SKILL.md overlay | plugin skill dir | `workflow_install.py` (unconditionally rewrites on install) | markdown | Yes | No — explicit scope exclusion: installs are operator-invoked, not unattended regeneration |
| Python CLI archive hook | `cmd_run` | future `_archive_markdown` | markdown | Yes | Deferred — slice 2 (live path never calls `cmd_run`) |

Round-1 finding, corrected per round-2 MINOR: a named `--output` already regenerates into a fixed path. The archive's role is therefore the DEFAULT stable briefing destination, not the first fixed path. The Goal is narrowed to match: the archive is the only protected surface in this slice.

## Vocabulary

| Term | Meaning |
|---|---|
| **Generated section** | A contiguous block of lines CoS's machinery wrote, delimited by marker lines. |
| **Marker** | A boundary line: `<!-- cos:generated <id> begin sha256=<12hex> -->` / `<!-- cos:generated <id> end -->` (HTML comment — inert in markdown). |
| **Operator text** | Any line outside generated sections. Never rewritten, never reordered, never deleted by regeneration. |
| **Span** | The (start, end) line range of one generated section, inclusive of both markers. |
| **Section registry** | `BRIEFING_ARCHIVE_SECTIONS` — one module-level ordered tuple in a module BOTH producers import. It is the ID set AND the position order. Only the merge API creates boundaries. |
| **Attribution merge API** | The single deterministic module (`shared/scripts/briefing_attribution.py`) owning parsing, validation, marker assembly, backups, and atomic replace. |
| **Quarantine** | A refused-mutation state: the merge exits non-zero without writing. The file is unchanged. |

## Section registry (round-2 B3, Codex #6 — published, not illustrative)

```python
BRIEFING_ARCHIVE_SECTIONS = (
    "header",
    "urgent",
    "calendar",
    "deadlines",
    "pipeline",
    "finance",
    "pending-high",
    "pending-medium",
    "pending-low",
    "todos",
    "inbox-summary",
    "all-clear",
    "footer",
)
```

- **Producer mapping (agent-composed, live path):** the skill's Output Format sections map 1:1 — Urgent→`urgent`, Calendar→`calendar`, Deadlines→`deadlines`, Pipeline→`pipeline`, Finance/AR→`finance`, To-Dos→`todos`, Inbox Summary→`inbox-summary`, All Clear→`all-clear`. Pending approvals emit per risk level: `pending-high` / `pending-medium` / `pending-low` (omitted levels are "not emitted", see lifecycle). `header` and `footer` are produced by the CLI path only; the agent envelope omits them.
- **Producer mapping (Python CLI, slice 2):** `render_markdown_sections(briefing) -> list[tuple[id, body]]` maps the renderer's blocks onto the same tuple; `render_markdown` becomes a join over it (byte-identical, regression-tested).
- **Lifecycle:** a registry id not emitted this run = **known-but-empty**: the span keeps its markers, body becomes `_(none today)_`. Never stale, never deleted. A file id NOT in the registry = unknown: untouched operator text, WARN in merge output. Undeclared id in an envelope: rejects the WHOLE envelope, exit 2, no write.
- **Retirement: DEFERRED.** Unknown = preserve is sufficient for additive evolution. (Both reviewers' recommendation.)

## Contract

### C-1: Marker format
- Begin: `<!-- cos:generated <section-id> begin sha256=<12hex> -->` — the hash covers the LF-normalized span body with trailing whitespace stripped per line, UTF-8 encoded, truncated to the first 12 hex chars of the sha256 digest. Pin the exact hashed byte sequence in a test.
- End: `<!-- cos:generated <section-id> end -->`
- `<section-id>`: `^[a-z0-9]+(-[a-z0-9]+)*$`, case-sensitive keywords, tolerant of `\s+` around fields. IDs come from the registry.
- Managed hash-field updates are permitted and are NOT a preservation violation: regeneration rewrites the begin marker's hash field when the body changes (this is the only managed change to a marker line).
- Unknown/malformed marker lines: fail-closed to preservation; the merge REFUSES the affected id (see C-6) and exits 2 for malformed input lines that cannot be parsed at all.

### C-2: Replacement scope
- The merge API parses the existing file, finds spans by exact section-id, and replaces ONLY span bodies (plus the managed begin-marker hash update).
- Content after a span's end marker is untouched (R7-1). Operator text between two spans is untouched (R7-3 corollary).
- Insertion anchor for a span absent from the old file: immediately after the end marker of the nearest earlier registry-declared span present in the file; if none, before the first later span; else end of file, with a blank-line separator inserted when the file does not end with a newline (round-2 Codex M9 boundary rule).

### C-3: Section lifecycle (minimal: known-but-empty only)
A registry id the producer did NOT emit this run: span keeps its markers, body becomes `_(none today)_`. Never stale, never deleted. **Retirement is deferred** (round-2 both reviewers): no registry removal, no meta line, no deletion path exists in slice 1. An unknown id in the file is preserved operator text with a WARN.

### C-4: Validation and marker integrity (round-2 Codex M5, Opus M5)
- **Two validators, two scopes — both named:**
  - Scalar values (titles, names, single-line fields): the existing shared scalar validator — `shared/scripts/runtime_log.py: sanitize_provider_error_detail` contract family; NEW function `sanitize_scalar(value)` added beside it (newline collapse: CR/LF/U+2028/2029 → space). Renderer purity unchanged; Telegram/text output unchanged.
  - Section bodies (multi-line, both producers): NEW merge-time body validator in `briefing_attribution.py`: `validate_body(body)` — applied line-by-line, PRESERVES structural line breaks, and neutralizes: `<!--` → `<!−−`, `-->` → `−−>`, and fence openers (a line whose first non-space chars are ``` or ~~~) → indented one space. (`&lt;` escaping is NOT used: it fails to neutralize `-->` and would double-escape in HTML.)
- Only the merge API creates marker lines.
- RED test: a calendar title containing a forged begin/end marker, regenerated twice, leaves operator text byte-identical. Multiline agent body with a mid-body forged marker: same.

### C-5: Write policy (round-2 M12/Codex #8/#9/#4)
- **Honest concurrency guarantee:** the `fcntl` lockfile at `project_root/.cos-briefing.lock` serializes COOPERATING writers (cron session, manual helper runs, future CLI hook). The lock is acquired BEFORE reading the merge base. Non-cooperating editors (vim, other processes) are NOT excluded: after acquiring the lock and reading the base, re-verify `st_mtime_ns` + size (metadata best-effort detection); on mismatch, REFUSE the file (exit 3), no backup, the operator edit survives. An edit landing between the re-check and rename is a residual accepted risk, stated as such.
- Lock acquisition: blocking with a 10s timeout; on timeout, exit 4 (`lock busy`), no partial state.
- Backup: required when the file has operator text outside spans (whitespace-only lines do NOT count), OR any hash mismatch, OR the file is being adopted (C-7). Hash mismatch OVERRIDES the pure-generated exclusion. Backup goes to `project_root/.cos-backups/attribution/` keyed by full artifact identity (never basename). Retention: 5 newest per artifact, pruned only after a successful commit. No backup for byte-identical no-ops.
- Transaction phases: acquire lock → read base → validate → write temp file in the same directory → backup the base (mandatory; on backup FAILURE, abort, no replace, WARN) → re-verify metadata → rename → prune → release lock. Tentative backups after a refusal are deleted.
- Byte-preservation: exact retention of operator-owned byte ranges. Supported encodings: UTF-8 with or without BOM; other encodings refused with exit 2. The merged file differs from the old file ONLY in: replaced span bodies, managed hash-field updates, inserted spans, inserted blank-line separators. CRLF and mixed endings are preserved per line. Legacy adoption of a file not ending in a newline inserts the separator before the first marker (Codex M9).

### C-6: Parser state machine (round-2 both reviewers — refusal instead of recovery)
- States: OUTSIDE → IN_SPAN(id) → END | EOF. Fence tracking is active ONLY in OUTSIDE state (a fence opens in operator text; if still open at EOF, re-parse without fence tracking).
- Markers inside properly-closed fenced code blocks are ignored (they are operator content).
- **Well-formed duplicates:** the LAST well-formed occurrence (begin…end, matching id, valid hash) is the live span and is replaced. Earlier occurrences are preserved as operator text, WARN. No span is ever appended for this class — this closes the growth loop by construction.
- **Any other ambiguity (nested begins, crossed ids, stray end marker, unclosed fence after re-parse):** the merge REFUSES the whole file: exit 2, no write, no backup, structured refusal reason in stdout JSON. The file is left byte-identical. Closure over 3+ cycles is trivially guaranteed by refusal; the freshly-generated content is delivered via the unmarked message as usual and reported as "archive skipped".
- Every refusal class is pinned over 3+ regeneration cycles with an assertion that line count stays bounded (by refusal, file size never grows).

### C-7: First-run contract (Codex r1#8, Opus m3)
A legacy file with no markers: preserve ALL existing content as operator text, append the freshly generated sections at EOF under their markers (with the C-2 EOF separator rule), WARN "legacy artifact adopted; previous generated content preserved". No claim of migration; no deletion. Second run: the appended spans are now recognized and replaced normally. Pinned over 3+ cycles.

### C-8: Diagnostics (lint folded into merge output — round-2 Opus cut list)
- The merge CLI returns, in its stdout JSON: per-section actions (replaced/inserted/empty/quarantined), WARNs (unknown ids, duplicates resolved, hash mismatches), the backup path taken, and refusal reasons. No separate lint command in slice 1.

### C-9: Helper CLI (round-2 Opus M9, Codex M7)
- Invocation: `briefing_attribution.py merge --artifact briefing --sections <envelope.json>`
- `--artifact briefing` resolves `project_root/briefing.md` from CoS config (paths.project_root). NO `--target` free-path flag: the agent cannot point the helper at a wiki page or SKILL.md.
- Envelope: `{"version": 1, "sections": {"<id>": "<markdown body>"}}`. Validation: `version == 1`, every key in `BRIEFING_ARCHIVE_SECTIONS`, values are strings. Undeclared id → exit 2. Missing vs empty section: both treated as "not emitted" (lifecycle known-but-empty).
- Envelope files live under `project_root/.cos-tmp/` (gitignored) and are DELETED after the merge — never `/tmp` (PII).
- Stdout JSON: `{status: merged|noop|refused|error, sections: {...}, warnings: [...], backup: <path|null>, archive: <path>}`. Exit codes: 0 = merged or no-op; 2 = validation reject / refusal; 3 = concurrent-edit refusal; 4 = lock busy.
- The helper writes a run-log entry on every merge (audit trail).

### C-10: Adoption detection (round-2 Opus M10)
- `doctor`/`readiness` reports "briefing archive last merged: <ts>" from the run log and WARNs when it is older than 36h while the archive is enabled. This institutionalizes the v0.5.x zero-live-data lesson.

## User Stories

### US-1: Operator annotates the briefing archive
Operator adds `> CHECK: confirm with bank` under the AR block in `project_root/briefing.md`. Next daily run regenerates through the helper. The annotation survives, verbatim, position unchanged. Regression matrix: annotation (a) immediately after an end marker, (b) between two sections, (c) at file end, (d) inside a span (hash detects it → conflict handling per C-4/US-1d below), (e) at file start, (f) empty-section between runs, (g) forged marker, (h) operator heading identical to a generated heading.
**US-1d (conflict handling, executable form):** hash mismatch on a well-formed span: move the operator-edited body VERBATIM above the fresh span as operator text, wrapped between `<!-- cos:conflict <id> <iso-date> -->` lines (which cannot match the C-1 marker regex), then write fresh content into the span. A mandatory backup is taken (C-5). The conflict does NOT re-trigger on cycle 2 (the operator text now sits outside the span). Pinned over 3+ cycles.

### US-2: Renderer adds a new section
A future CoS version adds a `trend-snapshot` id to the registry. Old artifacts lack its markers. The helper appends the new span at the C-2 anchor position without disturbing existing content.

### US-3: Agent-composed briefing adopts the helper (primary integration)
The daily-briefing skill instructs the composing session to write its section bodies to a JSON envelope at `project_root/.cos-tmp/briefing-sections.json` (schema per C-9) and invoke:
`briefing_attribution.py merge --artifact briefing --sections project_root/.cos-tmp/briefing-sections.json`
Python performs parsing, validation, backup, conflict handling, and atomic replace; the envelope is deleted after the merge. The agent NEVER performs the merge; the Telegram message stays unmarked text. Enforcement: (a) the helper's behaviour, (b) the skill text instructs the helper, (c) integration test — agent envelope merged into an annotated existing file, (d) cross-producer test — agent run then Python run against the same annotated archive (slice 2, after the CLI hook lands).

### US-4: Ambiguous markers refuse safely
An operator half-deletes an end marker (or duplicates a section by copy-paste). The C-6 state machine classifies the region; the merge REFUSES (exit 2), the file is byte-unchanged, the refusal reason names the class and line numbers, and the Telegram briefing still delivers unmarked. Pinned over 3+ cycles: line count bounded, operator text byte-identical.

### US-5: Concurrent edit (honest guarantee, round-2 Codex B4)
Guarantee tiers: (1) cooperating writers (two helper runs, cron + manual) NEVER lose an edit — `fcntl` serialization; (2) non-cooperating editors get best-effort detection — mtime_ns+size re-check after base read; on mismatch, refuse (exit 3) and the operator edit survives; (3) an edit between the re-check and rename is a residual risk, documented. Tests: edit before the re-check (refused), edit between check and rename (injection seam named: the re-verify accepts an injected callable, so the test is deterministic), two cooperating writers (serialized, both survive).

## Non-goals (slice 1)

- Text-format and HTML-format attribution; weekly review; JSON outputs (unchanged from v2).
- `--output` merging — stays a plain overwrite (markers must never leak into delivery).
- Retirement and the `generated-meta` line — unknown = preserve is sufficient; both reviewers' cut lists.
- Standalone lint command — diagnostics returned in merge output.
- Python `cmd_run` archive hook — slice 2; the envelope CLI is primary.
- Automatic recovery from ambiguous files — refusal replaces recovery (closes the growth loops by construction).
- No lumen runtime dependency; no retroactive legacy migration beyond C-7 preserve-and-append.

## Open questions — SETTLED (rounds 1-2)

1. **Section registry:** ONE shared `BRIEFING_ARCHIVE_SECTIONS` tuple in a module both producers import, published above with both producer mappings; undeclared envelope ids reject the whole envelope.
2. **Backup retention:** 5 newest per artifact under `project_root/.cos-backups/attribution/`, keyed by full artifact identity, pruned after success, skipped for byte-identical no-ops; hash-mismatch overrides the pure-generated exclusion (Codex M8).
3. **Backups vs git:** backups unconditional; on git-backed installs add `.cos-backups/` and `.cos-tmp/` and the lockfile to the data repo's `.gitignore`.

## Acceptance matrix (round-2 Codex M10, Opus test-plan gaps)

| # | Assertion |
|---|---|
| A1 | Operator annotations survive at all US-1 placements, byte-identical, position unchanged |
| A2 | Stdout, Telegram, and `--output` files contain no `cos:generated` markers ever |
| A3 | `render_markdown` output byte-identical after the section-list refactor (slice 2) |
| A4 | Idempotence: `merge(merge(f,s),s) == merge(f,s)` |
| A5 | Operator-text invariance as a property test under fuzzed placement |
| A6 | Cross-producer: agent envelope run then Python run on the same annotated archive (slice 2) |
| A7 | CRLF + mixed endings + no-trailing-newline + BOM round-trip with hash stability |
| A8 | Unclosed fence: refusal, byte-unchanged, bounded over 3+ cycles |
| A9 | Duplicate/nested/crossed/stray-end: refusal, byte-unchanged, bounded over 3+ cycles |
| A10 | Forged markers (begin/end/`-->` variants) through both producers: operator text byte-identical |
| A11 | Retired-span edit N/A in slice 1 (retirement deferred); unknown-id preservation pinned |
| A12 | Lock contention: second writer exits 4, no partial state |
| A13 | Backup: full-identity keys, 5-newest retention, failure aborts replace, same-basename collision distinct keys |
| A14 | Conflict-wrap: edited body preserved verbatim; no re-trigger on cycle 2 |
| A15 | Envelope: undeclared id → exit 2 no write; envelope deleted after merge; PII never in /tmp |
| A16 | Doctor: staleness WARN >36h while archive enabled |
| A17 | `--dry-run` writes no archive; delivery output unchanged |

## Build batches

- **Batch 1:** `briefing_attribution.py` — parser state machine with refusal-on-ambiguity, LF-normalized hash, C-2 anchors, C-3 known-but-empty, C-4 validators (scalar + body), C-5 write policy (lock, TOCTOU re-check, backups, transaction phases), C-7 first-run, C-8 diagnostics, C-9 CLI. Full test suite per acceptance matrix (A1-A15 minus A6/A16).
- **Batch 2:** skill adoption — SKILL.md envelope instruction + envelope-writing helper snippet + doctor staleness signal (A16) + `.gitignore` entries. Integration test A15 full path.
- **Batch 3 (slice 2, deferred):** `render_markdown_sections` refactor + `cmd_run` archive hook + `--dry-run` skip + cross-producer A6.

## Accepted deviations and deferred windows (build record)

Recorded at batch-2 close (2026-09-27). All were adjudicated by the orchestrator during review rounds; both review lanes confirmed SHIP.

1. **Envelope containment: warn-and-keep.** An envelope that resolves outside the project root is kept with a warning (not deleted). Codex batch-1 finding #9. Rationale: silent deletion of a file the operator may want to inspect is worse than a warning.
2. **Fence-indent warning.** Code-fence indentation in section bodies is preserved; a warning is emitted rather than an error. Confirmed in batch-1 confirm round.
3. **Absent-archive microsecond window (C-2, deferred).** Between the archive-appeared check and the final write, an external creator could theoretically race the helper. Atomic via `os.link` if ever needed. Known window, both lanes accepted as Low.
4. **Audit writer omits `reason`.** `_audit` never writes the `reason` field, so real failure entries have empty reasons. Doctor display now omits the trailing `: ` when empty (3ec7db0). A follow-up could add `reason` to `_audit`; requires unfreezing the module and is not needed for correct behavior.
5. **Undeclared-id stdout status.** Validation rejects return stdout status `error` (not `refused`); exit code 2 matches spec. Batch-2 adjudication: the pinned status is the batch-1 contract; refusal semantics are carried by the refusal statuses the helper does emit.
