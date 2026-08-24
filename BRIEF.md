# BRIEF: SQLite WAL State Store — Full Replacement

## Context

The Chief-of-Staff plugin (`/root/chief-of-staff-dev`) currently uses four file-based state stores with manual locking:

1. **state_store.py** (369 lines) — YAML stores for pipeline, invoices, expenses, todos
2. **pending_actions.py** (1017 lines) — JSON state machine for gated actions
3. **event_store.py** (353 lines) — JSON event ingestion with idempotency
4. **webhook_security.py** (442 lines) — JSON replay cache for webhook dedup

Each store implements the same pattern: load file → mutate in memory → fcntl flock → write `.tmp` → `os.replace` → `fsync`. The `file_lock.py` module (109 lines) provides the locking primitive. All four stores independently implement version counters, corruption detection, and atomic write logic.

The audit log (`workspace_audit.py` 215 lines, `audit_log.py` 156 lines) is append-only JSONL with a hash chain. It works correctly. **It stays as-is.** It is not part of this migration.

## Goal

Replace all four file-based state stores with a single SQLite database in WAL mode. This gives ACID transactions for free and retires `file_lock.py`, the `.tmp`+`replace`+`fsync` dance, and the version counter defense-in-depth.

## Approach: Full Replacement

Create a new `state_db.py` module with a SQLite-native API. Update all callers to use it. Delete the old file-based stores and `file_lock.py`.

## Database Schema

Single database file: `{project_root}/state.db`

### Table: `kv_stores`
```sql
CREATE TABLE kv_stores (
    store_name TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,  -- JSON-encoded value
    updated_at TEXT NOT NULL,
    PRIMARY KEY (store_name, key)
);
```
Replaces `state_store.py` YAML stores. Each store (pipeline, invoices, expenses, todos) stores its full document as a single JSON value under a fixed key (e.g. `__root__`). This preserves the load-all/save-all semantics callers expect.

### Table: `pending_actions`
```sql
CREATE TABLE pending_actions (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    provider TEXT NOT NULL,
    target TEXT NOT NULL,
    payload TEXT NOT NULL,      -- JSON
    summary TEXT,
    state TEXT NOT NULL,        -- requested|approved|executing|executed|cancelled|dismissed|expired|failed
    risk TEXT,                  -- JSON or NULL
    approver TEXT,
    approval_reason TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    executing_at TEXT,
    executed_at TEXT,
    cancelled_at TEXT,
    dismissed_at TEXT,
    expired_at TEXT,
    cancel_reason TEXT,
    dismiss_reason TEXT,
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,
    result TEXT                 -- JSON
);

CREATE INDEX idx_pa_state ON pending_actions(state);
CREATE INDEX idx_pa_created ON pending_actions(created_at);
```

### Table: `events`
```sql
CREATE TABLE events (
    id TEXT PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,   -- source:source_id idempotency key
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,      -- JSON
    summary TEXT,
    state TEXT NOT NULL,        -- received|classified|surfaced|processed
    classification TEXT,         -- JSON
    received_at TEXT NOT NULL,
    classified_at TEXT,
    surfaced_at TEXT,
    processed_at TEXT,
    processed_by TEXT,
    processing_notes TEXT
);

CREATE INDEX idx_ev_state ON events(state);
CREATE INDEX idx_ev_received ON events(received_at);
CREATE UNIQUE INDEX idx_ev_key ON events(key);
```

### Table: `webhook_replay`
```sql
CREATE TABLE webhook_replay (
    delivery_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,        -- processing|done
    ts REAL NOT NULL,           -- unix timestamp
    lease_token TEXT
);

CREATE INDEX idx_wr_ts ON webhook_replay(ts);
```

## New Module: `state_db.py`

Location: `shared/scripts/state_db.py`

```python
"""SQLite WAL state store — single DB for all Chief-of-Staff state.

Replaces state_store.py, pending_actions.py, event_store.py, and
webhook_security.py replay cache with ACID transactions.

Usage:
    from state_db import StateDB
    db = StateDB(config)           # opens {project_root}/state.db
    db.put_kv("pipeline", data)   # store pipeline as JSON
    data = db.get_kv("pipeline")  # load pipeline
"""

class StateDB:
    def __init__(self, config): ...
    # --- KV stores (replaces state_store.py) ---
    def get_kv(self, store_name: str) -> dict | None: ...
    def put_kv(self, store_name: str, data: dict, action: str | None = None, ...): ...
    # --- Pending actions (replaces pending_actions.py) ---
    def create_action(self, ...) -> dict | None: ...
    def get_action(self, action_id: str) -> dict | None: ...
    def list_actions(self, state: str | None = None, ...) -> list[dict]: ...
    def transition_action(self, action_id: str, new_state: str, **fields) -> dict | None: ...
    # --- Events (replaces event_store.py) ---
    def ingest_event(self, source: str, source_id: str, ...) -> dict | None: ...
    def get_event(self, event_id: str) -> dict | None: ...
    def list_events(self, ...) -> list[dict]: ...
    # --- Webhook replay (replaces webhook_security.py replay cache) ---
    def reserve_delivery(self, delivery_id: str, ttl: int) -> tuple[bool, str]: ...
    def complete_delivery(self, delivery_id: str, lease_token: str | None = None) -> None: ...
    def release_delivery(self, delivery_id: str, lease_token: str | None = None) -> None: ...
    def renew_delivery(self, delivery_id: str, lease_token: str | None = None) -> bool: ...
    # --- Maintenance ---
    def cleanup_old_events(self, days: int = 30) -> int: ...
    def cleanup_old_actions(self, days: int = 30) -> int: ...
    def cleanup_expired_replay(self, ttl: int) -> None: ...
    def close(self): ...
```

### Connection Management

- Each `StateDB` instance opens one `sqlite3.Connection` with `check_same_thread=False`
- WAL mode: `PRAGMA journal_mode=WAL`
- Foreign keys: `PRAGMA foreign_keys=ON`
- Busy timeout: `PRAGMA busy_timeout=10000` (10s, matches current lock timeout)
- Synchronous: `PRAGMA synchronous=NORMAL` (safe with WAL, fast)
- Connection is NOT a singleton. Callers create `StateDB(config)` per operation or per session. WAL mode allows concurrent readers + one writer without application-level locking.
- The `file_lock.py` module and all its callers are deleted.

### Transaction Pattern

All write operations use `with conn:` (BEGIN/COMMIT/ROLLBACK). SQLite handles concurrency via WAL + busy_timeout. The `_with_retry` + `ConcurrencyError` + `expected_version` pattern from the old code is retired — SQLite handles this natively.

For the pending-actions state machine transitions (approved → executing → executed), use `UPDATE ... WHERE id=? AND state=?` (atomic compare-and-swap). If `rowcount == 0`, the state changed concurrently — return `None`, same semantics as before.

### Auto-Migration

On first open, if `state.db` does not exist but legacy files are present in the project root, auto-migrate:
- `.pending_actions.json` → `pending_actions` table
- `.events.json` → `events` table
- `.webhook_replay_cache.json` → `webhook_replay` table
- `pipeline.yaml`, `invoices.yaml`, `expenses.yaml`, `todos.yaml` → `kv_stores` table

Migration runs inside a single transaction. Legacy files are renamed to `.migrated` suffix (not deleted) after successful migration. A `migration_log` table (or a row in `kv_stores`) records the migration timestamp.

### Schema Validation

The `schemas.py` module stays. `validate_store()` is called on KV data after loading from SQLite, same as it is called after loading from YAML today.

### Audit Integration

The `audit_log.py` and `workspace_audit.py` modules stay unchanged. `state_db.put_kv()` calls `append_audit()` the same way `save_store_atomic()` does today. The audit log remains JSONL on disk.

## Modules to Create

1. `shared/scripts/state_db.py` — the new SQLite state store (estimated 500-700 lines)

## Modules to Modify (caller updates)

### shared/scripts/
- `bookkeeper_actions.py` — replace `load_store`/`save_store_atomic` with `StateDB`
- `pipeline_actions.py` — replace `load_store`/`save_store_atomic`/`with_store_lock` with `StateDB`
- `suggested_actions.py` — replace `pending_actions.*` and `event_store.*` imports
- `memory.py` — replace `pending_actions.*` and `event_store.*` imports
- `briefing_sources.py` — replace `pending_actions.*` and `event_store.*` imports
- `review_queue.py` — replace `pending_actions.*` imports
- `email_classifier.py` — replace `pending_actions.*` imports
- `webhook_receiver.py` — replace `event_store.*` and `webhook_security.*` imports
- `doctor_base.py` — replace `webhook_security.validate_secret_config` (stays, just not from replay cache); update state file checks to point at `state.db`
- `chief_of_staff.py` — update references to `.pending_actions.json` in doctor/readiness checks

### skills/
- `pipeline-manager/scripts/pipeline.py` — replace `load_store`/`save_store_atomic`/`get_store_path`
- `todo-list/scripts/todo.py` — replace `load_store`/`save_store_atomic`
- `bookkeeper/scripts/invoices.py` — replace `load_store`/`save_store_atomic`
- `bookkeeper/scripts/invoice_ingest.py` — replace `load_store`, `event_store.*`, `pending_actions.*`
- `daily-briefing/scripts/daily_briefing.py` — replace `pending_actions.*`
- `document-preparer/scripts/delete_actions.py` — replace all `pending_actions.*` imports
- `document-preparer/scripts/webhook_events.py` — replace `pending_actions.*` and `event_store.*`
- `document-preparer/scripts/send_email.py` — replace `pending_actions.*`
- `document-preparer/scripts/poll_events.py` — replace `event_store.*`
- `document-preparer/scripts/event_actions.py` — replace `event_store.*`
- `email-organisation/scripts/email_organisation.py` — replace `pending_actions.*`
- `note-taker/scripts/wiki_curator.py` — replace `event_store.*`

## Modules to Delete

- `shared/scripts/state_store.py`
- `shared/scripts/pending_actions.py`
- `shared/scripts/event_store.py`
- `shared/scripts/file_lock.py`
- `shared/scripts/webhook_security.py` — the replay-cache functions move to `state_db.py`; the validation functions (validate_gmail_pubsub_payload, validate_calendar_headers, etc.) move to a new `webhook_validation.py`

## Modules that Stay Unchanged

- `shared/scripts/audit_log.py` — JSONL audit, not part of this migration
- `shared/scripts/workspace_audit.py` — JSONL hash chain, not part of this migration
- `shared/scripts/schemas.py` — validation stays, called after SQLite loads
- `shared/scripts/config.py` / `config_loader.py` — config stays
- `shared/scripts/runtime_log.py` — operational logging stays

## webhook_security.py Split

The current `webhook_security.py` has two concerns:
1. **Replay cache** (lines 210-397) — moves to `state_db.py`
2. **Payload/header validation** (lines 1-209, 400-442) — moves to new `webhook_validation.py`

Functions moving to `webhook_validation.py`:
- `get_webhook_secret()`, `sign_payload()`, `verify_signature()`
- `validate_gmail_pubsub_payload()`, `validate_calendar_headers()`, `validate_drive_headers()`
- `validate_secret_config()`
- All HMAC/OIDC validation logic

Callers of these validation functions are updated to import from `webhook_validation` instead of `webhook_security`.

## Public API Preservation

These public functions must remain available (same name, same signature, backed by SQLite):
- `classify_recipient_risk(recipient, config)` — stays in `state_db.py` or a shared utils module
- `classify_event(event_type, payload)` — stays in `state_db.py`
- `validate_secret_config()` — moves to `webhook_validation.py`

## Testing

### Existing Tests (must pass unchanged or with minimal import-path updates)

- `tests/test_state_store.py` (109 lines) — update imports to use `StateDB`
- `tests/test_pending_actions_v017.py` (526 lines) — update imports
- `tests/test_event_ingestion_v021.py` (335 lines) — update imports
- `tests/test_phase1_blocking.py` (366 lines) — update imports, keep B1/B2/B3 contract tests
- `tests/test_phase2_loop1.py` (482 lines) — update imports
- `tests/test_phase3_loop2.py` (336 lines) — update imports
- `tests/test_e2e_integration.py` (209 lines) — update imports
- `tests/test_workspace_audit.py` (70 lines) — no changes (audit stays)
- `tests/test_webhook_native_v020.py` — update imports
- `tests/test_webhook_hotfix_v0201.py` — update imports
- All other tests that import from the old modules

### New Tests (in `tests/test_state_db.py`)

Contract tests written by Hermes BEFORE Cursor builds:
- `test_db_creates_schema_on_first_open`
- `test_kv_store_roundtrip`
- `test_pending_action_full_lifecycle` (requested → approved → executing → executed)
- `test_pending_action_concurrent_transition` (two writers, only one wins)
- `test_event_idempotency`
- `test_webhook_replay_protection`
- `test_auto_migration_from_legacy_files`
- `test_wal_mode_enabled`
- `test_busy_timeout_set`
- `test_cleanup_old_events`
- `test_cleanup_old_actions`
- `test_corrupt_db_raises_not_silent_empty`

## Constraints

- **Stdlib only.** `sqlite3` is in the standard library. No new dependencies.
- **Python 3.11+** (matches `requires-python` in pyproject.toml).
- **No `file_lock.py`.** Delete it. SQLite WAL + busy_timeout replaces it.
- **No `.tmp`/`replace`/`fsync` dance.** SQLite handles durability.
- **Keep schema validation.** Call `validate_store()` after loading from SQLite.
- **Keep audit logging.** `audit_log.py` and `workspace_audit.py` stay as JSONL.
- **Keep the hash chain.** `workspace_audit.py` hash chain stays.
- **CI must pass.** Python 3.11 + 3.12 matrix, ruff lint, all tests green.
- **No behavior changes.** Same state machine, same transitions, same audit records. Only the storage layer changes.

## Verification Checks

1. `python -c "from state_db import StateDB; print('OK')"` — import check
2. `python -m pytest tests/test_state_db.py -v` — new tests pass
3. `python -m pytest -q` — full suite green
4. `ruff check shared/ skills/ hooks.py __init__.py` — lint clean
5. `python shared/scripts/doctor.py` — doctor still works (update state file checks)
6. Auto-migration test: create legacy files, open StateDB, verify data in SQLite

## Out of Scope

- M365 Graph code (Phase 4, blocked on Entra tenant)
- Audit log migration (stays JSONL)
- New features (rate limits, structured queryable audit, Graph notifications)
- Performance benchmarking (correctness first)