#!/usr/bin/env python3
"""Review-fix tests for Phase 5 SQLite state store (Codex findings)."""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

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


LEGACY_ACTION = {
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

LEGACY_EVENT = {
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


# ─── BLOCKING 1: mutate_kv ─────────────────────────────────────


def test_mutate_kv_serializes_read_modify_write(tmp_path):
    """Two concurrent mutate_kv calls must not drop either write."""
    from state_db import StateDB, mutate_kv

    config = cfg(tmp_path)
    db = StateDB(config)
    db.put_kv("todos", {"todos": []})
    db.close()

    errors: list[BaseException] = []

    def add(n: int) -> None:
        try:
            def _mut(data: dict) -> None:
                data.setdefault("todos", []).append(
                    {"id": f"t{n}", "title": f"Task {n}", "status": "open"}
                )

            mutate_kv("todos", _mut, config=config)
        except BaseException as exc:  # noqa: BLE001 — collect worker failures
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    db = StateDB(config)
    todos = (db.get_kv("todos") or {}).get("todos") or []
    ids = {t["id"] for t in todos}
    assert ids == {"t1", "t2"}
    db.close()


def test_mutate_kv_validates_before_write(tmp_path):
    from state_db import StateDB, SchemaError

    db = StateDB(cfg(tmp_path))
    db.put_kv("todos", {"todos": []})

    def _bad(data: dict) -> None:
        data.setdefault("todos", []).append({"id": "t1"})  # missing title/status

    with pytest.raises(SchemaError):
        db.mutate_kv("todos", _bad)
    assert db.get_kv("todos") == {"todos": []}
    db.close()


# ─── BLOCKING 2: idempotent migration ──────────────────────────


def test_migration_second_source_on_reopen(tmp_path):
    """A second legacy file added after first open is migrated on reopen."""
    from state_db import StateDB

    (tmp_path / ".pending_actions.json").write_text(json.dumps(LEGACY_ACTION))
    db = StateDB(cfg(tmp_path))
    assert db.get_action("abc123") is not None
    db.close()

    (tmp_path / ".events.json").write_text(json.dumps(LEGACY_EVENT))
    db2 = StateDB(cfg(tmp_path))
    events = db2.list_events(limit=10)
    assert len(events) == 1
    assert events[0]["source_id"] == "m1"
    assert not (tmp_path / ".events.json").exists()
    db2.close()


def test_migration_retries_rename_when_log_already_has_source(tmp_path):
    from state_db import StateDB

    (tmp_path / ".pending_actions.json").write_text(json.dumps(LEGACY_ACTION))
    db = StateDB(cfg(tmp_path))
    db.close()
    migrated = tmp_path / ".pending_actions.json.migrated"
    assert migrated.exists()

    # Simulate a crash between commit and rename: file reappears, log already has source.
    migrated.rename(tmp_path / ".pending_actions.json")
    db2 = StateDB(cfg(tmp_path))
    assert db2.get_action("abc123") is not None
    assert not (tmp_path / ".pending_actions.json").exists()
    db2.close()


def test_yaml_parse_error_raises_and_rolls_back(tmp_path):
    from state_db import StateDB, StateCorruptionError

    (tmp_path / ".pending_actions.json").write_text(json.dumps(LEGACY_ACTION))
    (tmp_path / "pipeline.yaml").write_text("deals: [\n  - {id: unterminated\n")
    with pytest.raises(StateCorruptionError):
        StateDB(cfg(tmp_path))
    # Transaction rolled back: the pending-actions source must not be logged.
    # Re-open after removing the bad YAML should still migrate the JSON.
    (tmp_path / "pipeline.yaml").unlink()
    db = StateDB(cfg(tmp_path))
    assert db.get_action("abc123") is not None
    db.close()


# ─── MAJOR 3: strict JSON / SchemaError ────────────────────────


def test_corrupt_kv_json_raises(tmp_path):
    from state_db import StateDB, StateCorruptionError, KV_ROOT_KEY

    db = StateDB(cfg(tmp_path))
    db.conn.execute(
        "INSERT OR REPLACE INTO kv_stores (store_name, key, value, updated_at) VALUES (?,?,?,?)",
        ("pipeline", KV_ROOT_KEY, "{not json", "2026-01-01T00:00:00+00:00"),
    )
    db.conn.commit()
    with pytest.raises(StateCorruptionError):
        db.get_kv("pipeline")
    db.close()


def test_get_kv_schema_error_not_treated_as_missing(tmp_path):
    from state_db import StateDB, SchemaError, KV_ROOT_KEY

    db = StateDB(cfg(tmp_path))
    db.conn.execute(
        "INSERT OR REPLACE INTO kv_stores (store_name, key, value, updated_at) VALUES (?,?,?,?)",
        ("pipeline", KV_ROOT_KEY, json.dumps({"deals": "not-a-list"}), "2026-01-01T00:00:00+00:00"),
    )
    db.conn.commit()
    with pytest.raises(SchemaError):
        db.get_kv("pipeline")
    db.close()


def test_list_pending_actions_corrupt_payload_raises(tmp_path):
    from state_db import StateDB, StateCorruptionError, list_pending_actions

    config = cfg(tmp_path)
    db = StateDB(config)
    action = db.create_action(type="mail.send", provider="google", target="a@x.com", payload={})
    db.conn.execute(
        "UPDATE pending_actions SET payload=? WHERE id=?",
        ("{bad", action["id"]),
    )
    db.conn.commit()
    db.close()
    with pytest.raises(StateCorruptionError):
        list_pending_actions(config)


# ─── MAJOR 4: lease token ──────────────────────────────────────


def test_reserve_delivery_returns_lease_token(tmp_path):
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    result = db.reserve_delivery("del-lease")
    ok, reason = result
    assert ok is True
    assert reason == "OK"
    assert result.lease_token
    db.close()


def test_stale_worker_complete_rejected_after_reclaim(tmp_path):
    from state_db import StateDB, PROCESSING_LEASE_SECONDS

    db = StateDB(cfg(tmp_path))
    first = db.reserve_delivery("del-reclaim")
    assert first[0] is True
    token_a = first.lease_token

    db.conn.execute(
        "UPDATE webhook_replay SET ts=? WHERE delivery_id=?",
        (time.time() - PROCESSING_LEASE_SECONDS - 10, "del-reclaim"),
    )
    db.conn.commit()

    second = db.reserve_delivery("del-reclaim")
    assert second[0] is True
    token_b = second.lease_token
    assert token_b != token_a

    db.complete_delivery("del-reclaim", lease_token=token_a)
    row = db.conn.execute(
        "SELECT state FROM webhook_replay WHERE delivery_id=?", ("del-reclaim",)
    ).fetchone()
    assert row["state"] == "processing"

    db.complete_delivery("del-reclaim", lease_token=None)
    row = db.conn.execute(
        "SELECT state FROM webhook_replay WHERE delivery_id=?", ("del-reclaim",)
    ).fetchone()
    assert row["state"] == "processing"

    db.release_delivery("del-reclaim", lease_token=None)
    row = db.conn.execute(
        "SELECT delivery_id FROM webhook_replay WHERE delivery_id=?", ("del-reclaim",)
    ).fetchone()
    assert row is not None

    db.complete_delivery("del-reclaim", lease_token=token_b)
    row = db.conn.execute(
        "SELECT state FROM webhook_replay WHERE delivery_id=?", ("del-reclaim",)
    ).fetchone()
    assert row["state"] == "done"
    db.close()


# ─── MAJOR 5: workspace audit lock ─────────────────────────────


def test_workspace_audit_lock_preserves_hash_chain(tmp_path):
    from workspace_audit import audit_workspace_action, verify_audit_chain

    config = cfg(tmp_path)
    errors: list[BaseException] = []

    def writer(i: int) -> None:
        try:
            audit_workspace_action(config, "test", f"op{i}", "tool", target=str(i))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert verify_audit_chain(config)
    log = tmp_path / ".audit" / "workspace.log"
    assert log.exists()
    assert len(log.read_text().strip().splitlines()) == 8


def test_statedb_audit_lock_context_manager(tmp_path):
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    with db.audit_lock():
        db.conn.execute("SELECT 1 FROM audit_lock")
    db.close()


# ─── MAJOR 6: BEGIN IMMEDIATE retry ────────────────────────────


def test_begin_immediate_retries_then_succeeds(tmp_path):
    from state_db import StateDB, _begin_immediate

    db = StateDB(cfg(tmp_path))
    n = {"c": 0}

    class _Conn:
        in_transaction = False

        def execute(self, sql, *a, **k):
            if isinstance(sql, str) and sql.strip().upper().startswith("BEGIN"):
                n["c"] += 1
                if n["c"] < 3:
                    raise sqlite3.OperationalError("database is locked")
            return db.conn.execute(sql, *a, **k)

        def rollback(self):
            return db.conn.rollback()

    _begin_immediate(_Conn())
    assert n["c"] == 3
    db.conn.commit()
    db.close()


def test_begin_immediate_exhausted_raises_concurrency_error(tmp_path):
    from state_db import ConcurrencyError, _begin_immediate

    n = {"c": 0}

    class _Conn:
        in_transaction = False

        def execute(self, sql, *a, **k):
            if isinstance(sql, str) and sql.strip().upper().startswith("BEGIN"):
                n["c"] += 1
                raise sqlite3.OperationalError("database is locked")
            raise AssertionError(f"unexpected SQL: {sql!r}")

        def rollback(self):
            return None

    with pytest.raises(ConcurrencyError):
        _begin_immediate(_Conn())
    assert n["c"] == 3


def test_save_does_not_swallow_begin_immediate_failure(tmp_path, monkeypatch):
    import state_db

    def boom(conn, retries=3):
        raise state_db.ConcurrencyError("locked")

    monkeypatch.setattr(state_db, "_begin_immediate", boom)
    with pytest.raises(state_db.ConcurrencyError):
        state_db._save(cfg(tmp_path), {"actions": {}, "_version": 0})


def test_save_events_does_not_swallow_begin_immediate_failure(tmp_path, monkeypatch):
    import state_db

    def boom(conn, retries=3):
        raise state_db.ConcurrencyError("locked")

    monkeypatch.setattr(state_db, "_begin_immediate", boom)
    with pytest.raises(state_db.ConcurrencyError):
        state_db._save_events(cfg(tmp_path), {"events": {}, "_version": 0})


# ─── MINOR 7: cleanup terminal states ──────────────────────────


def test_cleanup_old_actions_includes_dismissed_and_failed(tmp_path):
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    dismissed = db.create_action(type="mail.send", provider="google", target="a@x.com", payload={})
    db.transition_action(dismissed["id"], "dismissed", reason="nope")
    db.conn.execute(
        "UPDATE pending_actions SET dismissed_at=? WHERE id=?",
        ("2020-01-01T00:00:00+00:00", dismissed["id"]),
    )

    failed = db.create_action(type="mail.send", provider="google", target="b@x.com", payload={})
    db.transition_action(failed["id"], "approved", approver="MH")
    db.transition_action(failed["id"], "executing")
    db.transition_action(failed["id"], "failed", last_error="boom")
    db.conn.execute(
        "UPDATE pending_actions SET failed_at=? WHERE id=?",
        ("2020-01-01T00:00:00+00:00", failed["id"]),
    )
    db.conn.commit()

    removed = db.cleanup_old_actions(days=30)
    assert removed == 2
    assert db.get_action(dismissed["id"]) is None
    assert db.get_action(failed["id"]) is None
    db.close()


# ─── NIT 8: no duplicate idx_ev_key ────────────────────────────


def test_no_duplicate_ev_key_index(tmp_path):
    from state_db import StateDB

    db = StateDB(cfg(tmp_path))
    names = {
        row[0]
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
        )
    }
    assert "idx_ev_key" not in names
    db.close()
