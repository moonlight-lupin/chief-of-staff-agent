# CoS v0.5.0 Beta Review

## BLOCKING

### `shared/scripts/briefing_renderer.py:418-434` — attacker-controlled risk labels and link schemes allow XSS

`_risk_badge()` inserts `label = risk.title()` directly into HTML without escaping it. A briefing item whose `risk` is, for example, `<img src=x onerror=alert(1)>` produces executable markup. Separately, `_link()` HTML-escapes the URL but does not constrain its scheme, so an `event_link` or `conference_link` such as `javascript:alert(document.domain)` remains an executable link. Escaping attribute characters does not make a dangerous URI safe.

Suggested fix: escape the displayed risk label (or map unknown risks to a fixed `Info` label), and parse links through a URL allowlist that permits only expected schemes such as `https` (and possibly a deliberately supported provider-specific scheme). Reject or omit all other schemes. Add tests using malicious `risk`, `event_link`, `conference_link`, and generic `link` values, asserting that no executable markup or `javascript:` URL reaches the output.

### `skills/daily-briefing/scripts/daily_briefing.py:1035-1055` — advertised `--html` and `--output` features do nothing

The parser defines both flags at lines 854-855, but `cmd_run()` only branches on `--json` and `--markdown`; every other case renders text to stdout. It never reads `args.html` or `args.output_path`. Thus `run --html` emits plain text, and `run --output PATH` neither creates the file nor suppresses stdout. This is a complete failure of one of the release's headline user-facing features.

Suggested fix: determine one output format (including HTML, and the configured default where applicable), render once, then either write that string to the requested path or print it. Define and validate behavior for conflicting format flags (preferably a mutually exclusive argparse group). Add CLI-level tests that invoke `cmd_run()` or the script and verify the actual file/stdout contents, not merely parser acceptance.

## MAJOR

### `skills/daily-briefing/scripts/daily_briefing.py:1035-1102`, `shared/config/company.yaml.example:83` — `delivery.default_format` is dead configuration

The example configuration advertises `delivery.default_format: text | html`, but there is no production read of `default_format`. `run` always defaults to text and email notification always builds a Markdown body. Consequently selecting HTML in `company.yaml` has no effect, and the documented “html sends as file attachment” behavior is not implemented.

Suggested fix: centralize format selection from explicit CLI option first and `delivery.default_format` second, and implement the promised HTML delivery representation (including attachment metadata if that is the intended pending-action contract). Add tests for both supported config values and CLI override precedence.

### `shared/scripts/state_db.py:1399-1420, 1461-1473, 1562-1621`; `skills/pipeline-manager/scripts/pipeline.py:159-194` — legacy YAML remains an active second source of truth

The migration records YAML sources but deliberately leaves the files in place, contrary to the migration contract that legacy files are renamed to `.migrated`. Compatibility saves also continue rewriting YAML after committing SQLite, swallow YAML write errors, and `load_pipeline_store(strict=False)` bypasses SQLite and reads that YAML directly whenever it exists. A failed compatibility mirror write or a write performed through `StateDB.put_kv()` can therefore leave SQLite and YAML different; normal strict reads see the database while validation/listing paths can report stale YAML data. Keeping the original filename also means deleting/rebuilding `state.db` can silently re-import an obsolete snapshot.

Suggested fix: rename every successfully imported legacy YAML file after the migration transaction, stop treating it as live state, and make all read paths use SQLite. If human-readable exports are required, make them explicitly generated snapshots with a distinct name and never read them as authoritative state. Add a test that mutates SQLite without a YAML mirror and confirms strict and non-strict pipeline reads observe the same records.

### `shared/scripts/pipeline_actions.py:183-209`; `shared/scripts/bookkeeper_actions.py:487-535` — remaining load/modify/save call sites can lose concurrent updates

These mutation paths still call `load_store()`, modify the returned whole document, and later call `save_store_atomic()`. Each operation opens a separate connection, and `save_store_atomic()` performs an unconditional replacement, so the `BEGIN IMMEDIATE` protection in `mutate_kv()` does not cover the read-modify-write sequence. Two workers can load the same invoice or pipeline document, each append/update a different record, and the later save overwrites the earlier one. This negates the principal concurrency benefit of the WAL migration for these workflows.

Suggested fix: move the complete lookup/validation/mutation into a `mutate_kv()` callback for every write path (including duplicate checks whose result governs the write). Do not retain no-op `with_store_lock()` as an apparent concurrency primitive. Add a two-connection/thread regression test that forces overlapping mutations and verifies that both independent changes survive.

## MINOR

### `shared/scripts/state_db.py:1216-1231` — lease renewal accepts an omitted token for tokenized leases

`renew_delivery()` rejects a mismatch only when the caller supplies a truthy token. Passing `None` renews a row even when it has a stored lease token. This weakens the stated lease-token replay protection and is inconsistent with the token checks used by completion/release.

Suggested fix: when a stored token exists, require a non-null exact match. If tokenless legacy rows must remain supported, limit tokenless renewal to rows whose stored token is also null. Add coverage for `renew_delivery(id, None)` against a tokenized reservation.

## NIT

### `shared/scripts/pipeline_actions.py:11,183-200` and other migrated callers — stale module/YAML documentation obscures the new persistence contract

Several comments and docstrings still say mutations use `state_store` or operate on `pipeline.yaml`/`event_store`, even though imports now resolve to `state_db`. This makes it harder to identify the unsafe compatibility paths and reason about which store is authoritative.

Suggested fix: update documentation after the persistence behavior is finalized, explicitly distinguishing SQLite state from any optional export snapshots.

## Test note

The focused suites (`test_state_db.py`, `test_phase5_review.py`, `test_html_briefing.py`, and `test_attachment_hook.py`) pass (65 tests), but they currently miss the CLI execution behavior, hostile risk/URL values, divergent SQLite/YAML reads, and overlapping whole-document mutations described above.
