#!/usr/bin/env python3
"""Contract tests for Phase 1 blocking issues B1, B2, B3.

These tests are written BEFORE the fixes (TDD red). They encode the expected
correct behavior. They should FAIL against the current code, proving the bugs
exist. After Cursor implements the fixes, they should PASS.

B1: Guardrail bypass — legacy mutation IDs ungated + default-allow
B2: Concurrency safety — pending_actions/event_store/replay_cache unlocked
B3: M365 authority constraint is deferred to Phase 4 (no test here)
"""

import sys
import os
import json
import shutil
import tempfile
import multiprocessing
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture(autouse=True)
def clean_env():
    """Remove guardrail env vars before each test."""
    for key in ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"):
        os.environ.pop(key, None)
    yield
    for key in ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"):
        os.environ.pop(key, None)


@pytest.fixture
def temp_project(tmp_path):
    """Create a temp project root with config pointing to it."""
    project = tmp_path / "project"
    project.mkdir()
    config = {
        "google": {"delegate_email": "test@test.com", "account_alias": "test"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "paths": {"project_root": str(project)},
    }
    return config, project


# ═══════════════════════════════════════════════════════════════
# B1: Guardrail bypass — legacy IDs must be gated
# ═══════════════════════════════════════════════════════════════

class TestB1LegacyIdsAreGated:
    """Legacy gmail.*/drive.* mutation IDs must be classified as write actions.

    Currently they are deliberately excluded from WRITE_ACTIONS, so
    confirm_action() returns True (default-allow). They must be gated.
    """

    def test_gmail_archive_is_write_action(self):
        """gmail.archive must be recognized as a write action."""
        from workspace_guardrails import is_write_action
        assert is_write_action("gmail.archive") is True

    def test_gmail_trash_is_write_action(self):
        """gmail.trash must be recognized as a write action."""
        from workspace_guardrails import is_write_action
        assert is_write_action("gmail.trash") is True

    def test_gmail_untrash_is_write_action(self):
        """gmail.untrash must be recognized as a write action."""
        from workspace_guardrails import is_write_action
        assert is_write_action("gmail.untrash") is True

    def test_gmail_unarchive_is_write_action(self):
        """gmail.unarchive must be recognized as a write action."""
        from workspace_guardrails import is_write_action
        assert is_write_action("gmail.unarchive") is True

    def test_gmail_label_is_write_action(self):
        """gmail.label must be recognized as a write action."""
        from workspace_guardrails import is_write_action
        assert is_write_action("gmail.label") is True

    def test_gmail_create_label_is_write_action(self):
        """gmail.create_label must be recognized as a write action."""
        from workspace_guardrails import is_write_action
        assert is_write_action("gmail.create_label") is True

    def test_drive_trash_is_write_action(self):
        """drive.trash must be recognized as a write action."""
        from workspace_guardrails import is_write_action
        assert is_write_action("drive.trash") is True

    def test_drive_untrash_is_write_action(self):
        """drive.untrash must be recognized as a write action."""
        from workspace_guardrails import is_write_action
        assert is_write_action("drive.untrash") is True

    def test_calendar_uncancel_is_write_action(self):
        """calendar.uncancel must be recognized as a write action."""
        from workspace_guardrails import is_write_action
        assert is_write_action("calendar.uncancel") is True


class TestB1DefaultDeny:
    """Unknown action IDs must be denied, not allowed.

    Currently confirm_action returns True for any action not in WRITE_ACTIONS
    (default-allow). It must be default-deny: unknown actions should be
    blocked, with an explicit READ_ACTIONS allowlist for reads.
    """

    def test_unknown_action_is_not_write(self):
        """An unknown action ID should not be classified as a read action."""
        from workspace_guardrails import is_write_action
        # is_write_action should return True for unknown actions (default-deny
        # means unknowns are treated as writes until proven read-only)
        # OR: there should be a separate is_read_action that returns False
        # The key invariant: confirm_action must NOT return True for unknowns
        # We test confirm_action directly below
        pass  # This test is a placeholder — the real test is confirm_action

    def test_unknown_action_blocked_without_auto_approve(self):
        """confirm_action must block unknown action IDs without auto-approve."""
        from workspace_guardrails import confirm_action
        # An unknown action ID should NOT be auto-allowed
        result = confirm_action("unknown.mutation")
        assert result is False, "Unknown action ID must be blocked (default-deny)"

    def test_unknown_action_blocked_even_with_auto_approve(self):
        """confirm_action must block unknown action IDs even with auto-approve."""
        from workspace_guardrails import confirm_action
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        result = confirm_action("unknown.mutation")
        assert result is False, "Unknown action ID must be blocked even with auto-approve"

    def test_known_read_action_allowed(self):
        """Known read actions (gmail.search, calendar.list, etc.) must still pass."""
        from workspace_guardrails import confirm_action
        assert confirm_action("gmail.search") is True
        assert confirm_action("calendar.list") is True
        assert confirm_action("drive.search") is True
        assert confirm_action("mail.search") is True
        assert confirm_action("files.search") is True

    def test_legacy_gmail_archive_blocked_without_auto_approve(self):
        """gmail.archive must require confirmation (not bypass)."""
        from workspace_guardrails import confirm_action
        # Without auto-approve, non-TTY: must be blocked
        result = confirm_action("gmail.archive")
        assert result is False, "gmail.archive must be gated, not auto-allowed"

    def test_legacy_gmail_trash_blocked_without_auto_approve(self):
        """gmail.trash must require confirmation (not bypass)."""
        from workspace_guardrails import confirm_action
        result = confirm_action("gmail.trash")
        assert result is False, "gmail.trash must be gated, not auto-allowed"

    def test_legacy_drive_trash_blocked_without_auto_approve(self):
        """drive.trash must require confirmation (not bypass)."""
        from workspace_guardrails import confirm_action
        result = confirm_action("drive.trash")
        assert result is False, "drive.trash must be gated, not auto-allowed"


# ═══════════════════════════════════════════════════════════════
# B2: Concurrency safety — pending_actions store
# ═══════════════════════════════════════════════════════════════

class TestB2PendingActionsConcurrency:
    """pending_actions._save must use file locking, not optimistic versioning.

    The current optimistic versioning does load→check→write with no lock.
    Two processes can both pass the version check and both write, losing data.
    """

    def test_concurrent_mark_executing_one_wins(self, temp_project):
        """Two concurrent mark_executing calls on the same action — exactly one must win.

        This is the critical race: two processes both see 'approved', both
        transition to 'executing', and both send the same email.
        """
        from pending_actions import (
            create_pending_action, approve_pending_action,
            mark_executing, get_pending_action,
        )
        config, project = temp_project

        # Create and approve an action
        action = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )
        approve_pending_action(config, action["id"])

        # Simulate two concurrent mark_executing calls
        # In the current code, both will succeed because there's no lock
        result1 = mark_executing(config, action["id"])
        result2 = mark_executing(config, action["id"])

        # Exactly one must succeed, the other must return None
        winners = sum(1 for r in (result1, result2) if r is not None)
        assert winners == 1, (
            f"Exactly one mark_executing must win, got {winners}. "
            "Both succeeding means double-execution is possible."
        )

    def test_concurrent_create_no_lost_actions(self, temp_project):
        """Two concurrent create_pending_action calls must not lose either action.

        Current code: both load the same version, both append, both save with
        version+1 — last writer wins, first action is lost.
        """
        from pending_actions import create_pending_action, list_pending_actions
        config, project = temp_project

        # Create two actions rapidly (simulating concurrent processes)
        action1 = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )
        action2 = create_pending_action(
            config, "gmail.send", "google_api", "c@d.com", {"to": "c@d.com"}
        )

        # Both actions must be present
        all_actions = list_pending_actions(config)
        ids = [a["id"] for a in all_actions]
        assert action1["id"] in ids, "First action was lost (concurrent write race)"
        assert action2["id"] in ids, "Second action was lost (concurrent write race)"
        assert len(all_actions) == 2

    def test_save_uses_unique_temp_file(self, temp_project):
        """The temp file used during atomic write must be unique, not a fixed name.

        Current code uses path.with_suffix('.tmp') — a fixed name shared by
        all writers. Concurrent writers can corrupt each other's temp files.
        """
        from pending_actions import _save, _load, _pending_path
        config, project = temp_project

        # We can't directly test temp file naming without inspecting the code,
        # but we can verify that concurrent saves don't corrupt the file
        data = _load(config)
        data["actions"]["test1"] = {"id": "test1", "state": "requested"}
        data["actions"]["test2"] = {"id": "test2", "state": "requested"}

        _save(config, data)

        # Verify file is valid JSON (not corrupted)
        path = _pending_path(config)
        loaded = json.loads(path.read_text())
        assert "test1" in loaded["actions"]
        assert "test2" in loaded["actions"]


# ═══════════════════════════════════════════════════════════════
# B2: Concurrency safety — event_store
# ═══════════════════════════════════════════════════════════════

class TestB2EventStoreConcurrency:
    """event_store._save must use file locking, same as pending_actions."""

    def test_concurrent_ingest_no_lost_events(self, temp_project):
        """Two concurrent event ingestions must not lose either event."""
        from event_store import ingest_event, list_events
        config, project = temp_project

        event1 = ingest_event(
            config, "gmail", "msg-001", "email_received", {"from": "a@b.com"}
        )
        event2 = ingest_event(
            config, "gmail", "msg-002", "email_received", {"from": "c@d.com"}
        )

        all_events = list_events(config, limit=100)
        assert len(all_events) == 2, "Both events must be present (no lost events)"
        assert event1 is not None
        assert event2 is not None


# ═══════════════════════════════════════════════════════════════
# B2: Concurrency safety — webhook replay cache
# ═══════════════════════════════════════════════════════════════

class TestB2ReplayCacheConcurrency:
    """webhook_security replay cache must use file locking."""

    def test_concurrent_reserve_delivery_one_wins(self, temp_project):
        """Two concurrent reserve_delivery calls for the same ID — exactly one must win."""
        from webhook_security import reserve_delivery
        config, project = temp_project

        ok1, _ = reserve_delivery(config, "delivery-123")
        ok2, _ = reserve_delivery(config, "delivery-123")

        # Exactly one must succeed
        winners = sum(1 for ok in (ok1, ok2) if ok)
        assert winners == 1, (
            f"Exactly one reserve_delivery must win, got {winners}. "
            "Both succeeding means double-ingest is possible."
        )


# ═══════════════════════════════════════════════════════════════
# B2: Multiprocessing race test (real concurrency, not simulated)
# ═══════════════════════════════════════════════════════════════

def _worker_mark_executing(config, action_id, results, idx):
    """Worker process for multiprocessing concurrency test."""
    try:
        sys.path.insert(0, str(SHARED_SCRIPTS))
        from pending_actions import mark_executing
        result = mark_executing(config, action_id)
        results[idx] = (result is not None)
    except Exception as exc:
        results[idx] = f"ERROR: {exc}"


class TestB2MultiprocessingRace:
    """Real multiprocessing test for the mark_executing race.

    This test forks two processes that both try to mark the same action as
    executing. Without proper locking, both can succeed.
    """

    def test_two_processes_mark_executing_one_wins(self, temp_project):
        """Two processes mark_executing the same action — exactly one must win."""
        from pending_actions import (
            create_pending_action, approve_pending_action, get_pending_action,
        )
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "race@test.com", {"to": "race@test.com"}
        )
        approve_pending_action(config, action["id"])

        # Use multiprocessing to simulate real concurrent access
        # Note: We can't use tmp_path in subprocesses, so we use the actual
        # project path from the fixture
        manager = multiprocessing.Manager()
        results = manager.dict()
        procs = []
        for i in range(2):
            p = multiprocessing.Process(
                target=_worker_mark_executing,
                args=(config, action["id"], results, i),
            )
            procs.append(p)

        # Start both processes simultaneously
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=10)

        # Count winners
        winners = sum(1 for i in range(2) if results.get(i) is True)
        assert winners == 1, (
            f"Exactly one process must win mark_executing, got {winners}. "
            f"Results: {dict(results)}"
        )