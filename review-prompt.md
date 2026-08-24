# Review Task: CoS v0.5.0 Beta — Full Review

Review the diff at /tmp/v050-review.diff (v0.4.0..HEAD, 82 files, 5558+/3132-).

This release has two major feature areas:

## Feature 1: SQLite WAL State Store (Phase 5)
Full replacement of 4 file-based state stores (state_store.py, pending_actions.py, event_store.py, webhook_security.py) with a single SQLite WAL database (state_db.py, ~2000 lines).

Key areas to review:
- Transaction safety: `mutate_kv()` with BEGIN IMMEDIATE, `transition_action()` CAS
- Auto-migration of legacy JSON/YAML on first open (renamed to .migrated)
- Strict corruption detection: `_loads()` raises StateCorruptionError
- Lease token replay protection
- `_save()` event handling (BEGIN IMMEDIATE failure must not be swallowed)
- Cleanup includes all terminal states (dismissed, failed, approved-after-retry)
- `_ALLOWED_CAS_COLUMNS` frozenset validates column names before SQL interpolation
- Compat functions (create_pending_action, mark_executed, mark_failed, etc.) delegate to `transition_action()` via `_call_db` — one state-machine implementation, not two
- `transition_action("failed")` enforces MAX_RETRIES cap

## Feature 2: HTML Briefing + Attachment Hook
- `render_html()` in briefing_renderer.py — self-contained HTML, inline CSS, collapsible sections, risk badges, dark-mode, no JavaScript
- `event_link` field added to event schema (schemas.py) + normalize_event/normalize_message/normalize_file
- `attachment_drive_suggestion()` hook in hooks.py — post_llm_call, detects attachments, classifies by extension, suggests filing with user confirmation (never auto-uploads)
- `--html` and `--output` flags on daily_briefing.py run subcommand
- delivery.default_format and hooks.attachment_suggestions config options

## Review Focus
1. **Security**: SQL injection in _cas_update (column allowlist), XSS in render_html (HTML escaping), hook safety (no auto-upload)
2. **Correctness**: transaction semantics, CAS state transitions, migration data loss scenarios
3. **Edge cases**: empty briefing HTML, missing event_link, concurrent access, corrupt JSON
4. **Code quality**: dead code, error handling, naming, test coverage gaps
5. **Breaking changes**: any callers that still reference deleted modules?

## Output Format

Write your full review to /tmp/v050-cursor-review.md with:
- BLOCKING: must fix before ship
- MAJOR: should fix before ship
- MINOR: polish, not blocking
- NIT: style/cleanup

For each issue: file, line (approx), description, suggested fix.

Do NOT make file changes — review only. Read the actual source files for context, not just the diff.