#!/usr/bin/env python3
"""Contract tests for SQLite WAL state store (Phase 5).

These tests are written BEFORE the implementation (TDD red). They encode the
expected behavior of the new state_db.py module. They should FAIL until
state_db.py is implemented.

Covers:
- Schema creation on first open
- KV store roundtrip (replaces state_store.py)
- Pending action full lifecycle (replaces pending_actions.py)
- Concurrent transition safety (atomic compare-and-swap)
- Event idempotency (replaces event_store.py)
- Webhook replay protection (replaces webhook_security.py replay cache)
- Auto-migration from legacy JSON/YAML files
- WAL mode and pragmas
- Cleanup of old events/actions
- Corruption detection (fail closed, not silent empty)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def cfg(root: Path) -> dict:
    return {
        "paths": {"project_root": str(root)},
        "sales_stages": ["Lead", "Proposal Sent", "Paid"],
    }


# ─── Schema creation ────────────────────────────────────────────


def test_db_creates_schema_on_first_open(tmp_path):
    """Opening StateDB on a fresh project root creates state.db with all tables."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    assert (tmp_path / "state.db").exists()

    # All four tables exist
    tables = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = {t[0] for t in tables}
    assert "pending_actions" in table_names
    assert "events" in table_names
    assert "webhook_replay" in table_names
    # KV stores table (name may vary — check for at least one kv-like table)
    assert any("kv" in t.lower() or "store" in t.lower() for t in table_names)
    db.close()


# ─── KV store roundtrip ─────────────────────────────────────────


def test_kv_store_roundtrip(tmp_path):
    """KV store save/load roundtrip works for all four store types."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))

    # Pipeline
    pipeline_data = {"deals": [{"id": "d1", "client_name": "Acme", "stage": "Lead"}]}
    db.put_kv("pipeline", pipeline_data)
    loaded = db.get_kv("pipeline")
    assert loaded is not None
    assert loaded == pipeline_data

    # Invoices
    inv_data = {"invoices": [{"id": "inv1", "client": "Acme", "amount": "1000"}]}
    db.put_kv("invoices", inv_data)
    assert db.get_kv("invoices") == inv_data

    # Todos
    todo_data = {"todos": [{"id": "t1", "title": "Test"}]}
    db.put_kv("todos", todo_data)
    assert db.get_kv("todos") == todo_data

    # Expenses
    exp_data = {"expenses": [{"id": "e1", "amount": "50"}]}
    db.put_kv("expenses", exp_data)
    assert db.get_kv("expenses") == exp_data
    db.close()


def test_kv_store_missing_returns_none(tmp_path):
    """Loading a non-existent KV store returns None (or empty template)."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    result = db.get_kv("nonexistent")
    assert result is None or result == {}
    db.close()


def test_kv_store_overwrite(tmp_path):
    """Overwriting a KV store replaces the old data entirely."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    db.put_kv("pipeline", {"deals": [{"id": "d1"}]})
    db.put_kv("pipeline", {"deals": [{"id": "d2"}]})
    loaded = db.get_kv("pipeline")
    assert loaded == {"deals": [{"id": "d2"}]}
    db.close()


# ─── Pending action lifecycle ────────────────────────────────────


def test_pending_action_full_lifecycle(tmp_path):
    """Full lifecycle: create → approve → mark_executing → mark_executed."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))

    # Create
    action = db.create_action(
        type="mail.send",
        provider="google",
        target="user@example.com",
        payload={"subject": "Test", "body": "Hello"},
    )
    assert action is not None
    assert action["state"] == "requested"
    action_id = action["id"]

    # Get
    fetched = db.get_action(action_id)
    assert fetched is not None
    assert fetched["type"] == "mail.send"

    # Approve
    approved = db.transition_action(action_id, "approved", approver="MH", reason="OK")
    assert approved is not None
    assert approved["state"] == "approved"
    assert approved["approver"] == "MH"

    # Mark executing
    executing = db.transition_action(action_id, "executing")
    assert executing is not None
    assert executing["state"] == "executing"

    # Mark executed
    executed = db.transition_action(action_id, "executed", result={"success": True})
    assert executed is not None
    assert executed["state"] == "executed"
    assert executed["result"] == {"success": True}
    db.close()


def test_pending_action_list_filtered_by_state(tmp_path):
    """list_actions with state filter returns only matching actions."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    a1 = db.create_action(type="mail.send", provider="google", target="a@x.com", payload={})
    a2 = db.create_action(type="mail.send", provider="google", target="b@x.com", payload={})
    db.transition_action(a1["id"], "approved", approver="MH")

    requested = db.list_actions(state="requested")
    assert len(requested) == 1
    assert requested[0]["id"] == a2["id"]

    approved = db.list_actions(state="approved")
    assert len(approved) == 1
    assert approved[0]["id"] == a1["id"]
    db.close()


# ─── Concurrent transition safety ───────────────────────────────


def test_concurrent_transition_only_one_wins(tmp_path):
    """Two concurrent approved→executing transitions: only one succeeds."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    action = db.create_action(
        type="mail.send", provider="google", target="a@x.com", payload={}
    )
    db.transition_action(action["id"], "approved", approver="MH")

    # Open a second connection (simulating a second process)
    db2 = StateDB(cfg(tmp_path))

    # Both try to transition approved → executing
    # The compare-and-swap (WHERE state='approved') ensures only one wins
    r1 = db.transition_action(action["id"], "executing")
    r2 = db2.transition_action(action["id"], "executing")

    # Exactly one should succeed, one should return None
    assert (r1 is not None) ^ (r2 is not None), "Exactly one transition must win"
    db.close()
    db2.close()


# ─── Event idempotency ──────────────────────────────────────────


def test_event_idempotency(tmp_path):
    """Ingesting the same source+source_id twice returns None the second time."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))

    event1 = db.ingest_event(
        source="gmail",
        source_id="msg123",
        event_type="email_received",
        payload={"subject": "Test"},
    )
    assert event1 is not None
    assert event1["id"] is not None

    event2 = db.ingest_event(
        source="gmail",
        source_id="msg123",  # same key
        event_type="email_received",
        payload={"subject": "Test"},
    )
    assert event2 is None  # duplicate, idempotent
    db.close()


def test_event_list_and_mark_processed(tmp_path):
    """Events can be listed and marked processed."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    db.ingest_event("gmail", "m1", "email_received", {"s": "1"})
    db.ingest_event("gmail", "m2", "email_received", {"s": "2"})

    events = db.list_events(limit=10)
    assert len(events) == 2

    # Mark one as processed
    first_id = events[0]["id"]
    db.mark_event_processed(first_id, processed_by="agent")

    processed = db.list_events(state="processed")
    assert len(processed) == 1
    assert processed[0]["processed_by"] == "agent"
    db.close()


# ─── Webhook replay protection ──────────────────────────────────


def test_webhook_replay_first_delivery_allowed(tmp_path):
    """First delivery of a webhook is allowed (not a replay)."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    ok, reason = db.reserve_delivery("del-1", ttl=3600)
    assert ok is True
    db.close()


def test_webhook_replay_duplicate_blocked(tmp_path):
    """Second delivery of same ID while processing is blocked as replay."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    db.reserve_delivery("del-1", ttl=3600)
    ok, reason = db.reserve_delivery("del-1", ttl=3600)
    assert ok is False
    assert "replay" in reason.lower() or "already" in reason.lower()
    db.close()


def test_webhook_replay_complete_then_replay_blocked(tmp_path):
    """After completion, a re-delivery is blocked as replay."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    db.reserve_delivery("del-1", ttl=3600)
    db.complete_delivery("del-1")
    ok, reason = db.reserve_delivery("del-1", ttl=3600)
    assert ok is False
    assert "replay" in reason.lower() or "already" in reason.lower()
    db.close()


def test_webhook_replay_release_allows_retry(tmp_path):
    """After release, the same delivery ID can be reserved again."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    db.reserve_delivery("del-1", ttl=3600)
    db.release_delivery("del-1")
    ok, _ = db.reserve_delivery("del-1", ttl=3600)
    assert ok is True
    db.close()


# ─── Auto-migration ─────────────────────────────────────────────


def test_auto_migration_from_legacy_files(tmp_path):
    """Opening StateDB with existing legacy files auto-migrates them to SQLite."""
    from state_db import StateDB

    # Create legacy pending_actions.json
    legacy_pa = {
        "actions": {
            "abc123": {
                "id": "abc123",
                "type": "mail.send",
                "provider": "google",
                "target": "a@x.com",
                "payload": {"subject": "Hi"},
                "summary": "test",
                "state": "requested",
                "created_at": "2026-01-01T00:00:00+00:00",
                "approved_at": None,
                "executed_at": None,
                "cancelled_at": None,
                "dismissed_at": None,
                "expired_at": None,
                "result": None,
                "approver": None,
                "approval_reason": None,
                "risk": None,
            }
        },
        "_version": 5,
    }
    (tmp_path / ".pending_actions.json").write_text(json.dumps(legacy_pa))

    # Create legacy events.json
    legacy_events = {
        "events": {
            "gmail:m1": {
                "id": "evt1",
                "key": "gmail:m1",
                "source": "gmail",
                "source_id": "m1",
                "event_type": "email_received",
                "payload": {"subject": "Test"},
                "summary": "test event",
                "state": "classified",
                "classification": {"category": "email_received"},
                "received_at": "2026-01-01T00:00:00+00:00",
                "classified_at": "2026-01-01T00:00:00+00:00",
                "surfaced_at": None,
                "processed_at": None,
                "processed_by": None,
                "processing_notes": None,
            }
        },
        "_version": 3,
    }
    (tmp_path / ".events.json").write_text(json.dumps(legacy_events))

    # Open DB — should auto-migrate
    db = StateDB(cfg(tmp_path))

    # Verify migration happened
    action = db.get_action("abc123")
    assert action is not None
    assert action["type"] == "mail.send"
    assert action["state"] == "requested"

    event = db.list_events(limit=10)
    assert len(event) == 1
    assert event[0]["source_id"] == "m1"

    # Legacy files should be renamed (not deleted)
    assert not (tmp_path / ".pending_actions.json").exists()
    assert (tmp_path / ".pending_actions.json.migrated").exists() or (
        tmp_path / ".pending_actions.json.bak"
    ).exists()

    db.close()


# ─── WAL mode and pragmas ───────────────────────────────────────


def test_wal_mode_enabled(tmp_path):
    """Database is opened in WAL mode."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    mode = db.conn.execute("PRAGMA journal_mode").fetchone()
    assert mode[0].lower() == "wal"
    db.close()


def test_busy_timeout_set(tmp_path):
    """Busy timeout is set to at least 5 seconds."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    timeout = db.conn.execute("PRAGMA busy_timeout").fetchone()
    assert timeout[0] >= 5000  # milliseconds
    db.close()


def test_foreign_keys_on(tmp_path):
    """Foreign key enforcement is enabled."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    fk = db.conn.execute("PRAGMA foreign_keys").fetchone()
    assert fk[0] == 1
    db.close()


# ─── Cleanup ────────────────────────────────────────────────────


def test_cleanup_old_events(tmp_path):
    """cleanup_old_events removes processed events older than N days."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    db.ingest_event("gmail", "m1", "email_received", {})
    events = db.list_events(limit=10)
    # Manually mark processed with old timestamp
    db.conn.execute(
        "UPDATE events SET state='processed', processed_at=? WHERE id=?",
        ("2020-01-01T00:00:00+00:00", events[0]["id"]),
    )
    db.conn.commit()

    removed = db.cleanup_old_events(days=30)
    assert removed == 1
    assert len(db.list_events(limit=10)) == 0
    db.close()


def test_cleanup_old_actions(tmp_path):
    """cleanup_old_actions removes terminal actions older than N days."""
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    action = db.create_action(
        type="mail.send", provider="google", target="a@x.com", payload={}
    )
    db.transition_action(action["id"], "approved", approver="MH")
    db.transition_action(action["id"], "executing")
    db.transition_action(action["id"], "executed", result={"success": True})

    # Set old executed_at
    db.conn.execute(
        "UPDATE pending_actions SET executed_at=? WHERE id=?",
        ("2020-01-01T00:00:00+00:00", action["id"]),
    )
    db.conn.commit()

    removed = db.cleanup_old_actions(days=30)
    assert removed == 1
    assert db.get_action(action["id"]) is None
    db.close()


# ─── Corruption detection ──────────────────────────────────────


def test_corrupt_db_raises_not_silent(tmp_path):
    """A corrupt state.db raises an error, not silently returns empty."""
    from state_db import StateDB

    # Write garbage to state.db
    (tmp_path / "state.db").write_bytes(b"not a database")

    with pytest.raises(Exception):
        StateDB(cfg(tmp_path))