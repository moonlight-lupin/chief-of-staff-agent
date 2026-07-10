#!/usr/bin/env python3
"""Tests for v0.1.19 — execution-state hardening and restore workflows.

Verifies:
- mark_executing() checks expiry BEFORE provider call (race fix)
- Expired approvals never call provider methods
- State machine: approved → executing → executed | failed
- mark_failed() transitions back to approved for retry
- Restore commands work for gmail archive/trash and calendar cancel
- assert_executable() checks without state change
"""

import sys
import os
import json
import io
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILLS_SCRIPTS = PLUGIN_ROOT / "skills"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
for skill in ("document-preparer",):
    d = SKILLS_SCRIPTS / skill / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


@pytest.fixture
def temp_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = {
        "google": {"delegate_email": "test@test.com", "account_alias": "test",
                   "domain": "phronesis-applied.com"},
        "company": {"website": "phronesis-applied.com"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "paths": {"project_root": str(project)},
    }
    return config, project


@pytest.fixture
def auto_approve():
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
    yield
    os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)


@pytest.fixture
def google_mock():
    mock = MagicMock()
    mock.provider_name = "google_api"
    mock.supports.side_effect = lambda action: True
    mock.gmail_send.return_value = {"success": True, "action": "gmail.send", "provider": "google_api", "data": {"id": "msg1"}, "audited": True}
    mock.gmail_archive.return_value = {"success": True, "action": "gmail.archive", "provider": "google_api", "data": {"reversible": True}, "audited": True}
    mock.gmail_trash.return_value = {"success": True, "action": "gmail.trash", "provider": "google_api", "data": {"reversible": True}, "audited": True}
    mock.gmail_unarchive.return_value = {"success": True, "action": "gmail.unarchive", "provider": "google_api", "data": {}, "audited": True}
    mock.gmail_untrash.return_value = {"success": True, "action": "gmail.untrash", "provider": "google_api", "data": {}, "audited": True}
    mock.calendar_cancel.return_value = {"success": True, "action": "calendar.cancel", "provider": "google_api", "data": {"reversible": True}, "audited": True}
    mock.calendar_uncancel.return_value = {"success": True, "action": "calendar.uncancel", "provider": "google_api", "data": {}, "audited": True}
    return mock


def _age_approved(config, action_id, hours_old):
    from pending_actions import _load, _save
    data = _load(config)
    expected_version = data.get("_version", 0)
    data["actions"][action_id]["approved_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=hours_old)
    ).isoformat()
    _save(config, data, expected_version=expected_version)


# ─── Execution State Machine ─────────────────────────────────

class TestExecutionStateMachine:
    """Test approved → executing → executed | failed transitions."""

    def test_mark_executing_transitions_state(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executing, get_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        executing = mark_executing(config, action["id"])
        assert executing is not None
        assert executing["state"] == "executing"
        assert executing.get("executing_at") is not None

    def test_mark_executed_requires_executing_state(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executed
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        # mark_executed without mark_executing first should fail
        result = mark_executed(config, action["id"], {"success": True})
        assert result is None  # not in executing state

    def test_full_state_machine(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executing, mark_executed, get_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        mark_executing(config, action["id"])
        mark_executed(config, action["id"], {"success": True})
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "executed"

    def test_mark_failed_back_to_approved(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executing, mark_failed, get_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        mark_executing(config, action["id"])
        failed = mark_failed(config, action["id"], "API timeout")
        assert failed["state"] == "approved"  # back to approved for retry
        assert failed["last_error"] == "API timeout"
        assert failed["retry_count"] == 1

    def test_retry_after_failure(self, temp_project):
        """After mark_failed, can mark_executing again and then mark_executed."""
        from pending_actions import create_pending_action, approve_pending_action, mark_executing, mark_failed, mark_executed, get_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        mark_executing(config, action["id"])
        mark_failed(config, action["id"], "timeout")
        # Retry
        mark_executing(config, action["id"])
        mark_executed(config, action["id"], {"success": True})
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "executed"
        assert loaded["retry_count"] == 1


# ─── Pre-Execution Expiry Check (Race Fix) ────────────────────

class TestPreExecutionExpiryCheck:
    """The critical race fix: expired approval must never call provider."""

    def test_mark_executing_rejects_lapsed(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executing, APPROVED_EXPIRY_HOURS, get_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        _age_approved(config, action["id"], APPROVED_EXPIRY_HOURS + 1)
        result = mark_executing(config, action["id"])
        assert result is None  # rejected
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "expired"

    def test_send_email_execute_never_calls_provider_when_expired(self, temp_project, google_mock, auto_approve):
        """The critical test: expired approval must never call gmail_send."""
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            from pending_actions import create_pending_action, approve_pending_action, APPROVED_EXPIRY_HOURS
            action = create_pending_action(config, "gmail.send", "google_api", "a@b.com",
                                           {"to": "a@b.com", "subject": "S", "body": "B", "cc": ""})
            approve_pending_action(config, action["id"])
            _age_approved(config, action["id"], APPROVED_EXPIRY_HOURS + 1)
            rc = send_email.main(["execute", "--action-id", action["id"]])
        assert rc == 1
        google_mock.gmail_send.assert_not_called()  # NEVER called

    def test_delete_execute_never_calls_provider_when_expired(self, temp_project, google_mock, auto_approve):
        """Same for delete_actions: expired approval must never call provider."""
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            from pending_actions import create_pending_action, approve_pending_action, APPROVED_EXPIRY_HOURS
            action = create_pending_action(config, "gmail.archive", "google_api", "msg123",
                                           {"reason": "old", "reversible": True,
                                            "restore_hint": "add INBOX", "provider_method": "gmail_archive"})
            approve_pending_action(config, action["id"])
            _age_approved(config, action["id"], APPROVED_EXPIRY_HOURS + 1)
            rc = delete_actions.main(["execute", "--action-id", action["id"]])
        assert rc == 1
        google_mock.gmail_archive.assert_not_called()  # NEVER called

    def test_assert_executable_returns_action(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, assert_executable
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        result = assert_executable(config, action["id"])
        assert result is not None
        assert result["state"] == "approved"  # no state change

    def test_assert_executable_rejects_lapsed(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, assert_executable, APPROVED_EXPIRY_HOURS
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        _age_approved(config, action["id"], APPROVED_EXPIRY_HOURS + 1)
        result = assert_executable(config, action["id"])
        assert result is None


# ─── Restore Workflows ────────────────────────────────────────

class TestRestoreWorkflows:
    """Test restore commands for soft-delete actions."""

    def test_restore_gmail_archive(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            from pending_actions import create_pending_action, approve_pending_action, mark_executing, mark_executed
            # Create and execute an archive action
            action = create_pending_action(config, "gmail.archive", "google_api", "msg123",
                                           {"reason": "old", "reversible": True,
                                            "restore_hint": "add INBOX", "provider_method": "gmail_archive"})
            approve_pending_action(config, action["id"])
            mark_executing(config, action["id"])
            mark_executed(config, action["id"], {"success": True})
            # Restore
            rc = delete_actions.main(["restore", "--action-id", action["id"]])
        assert rc == 0
        google_mock.gmail_unarchive.assert_called_once_with("msg123")

    def test_restore_gmail_trash(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            from pending_actions import create_pending_action, approve_pending_action, mark_executing, mark_executed
            action = create_pending_action(config, "gmail.trash", "google_api", "msg456",
                                           {"reason": "spam", "reversible": True,
                                            "restore_hint": "remove TRASH", "provider_method": "gmail_trash"})
            approve_pending_action(config, action["id"])
            mark_executing(config, action["id"])
            mark_executed(config, action["id"], {"success": True})
            rc = delete_actions.main(["restore", "--action-id", action["id"]])
        assert rc == 0
        google_mock.gmail_untrash.assert_called_once_with("msg456")

    def test_restore_calendar_cancel(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            from pending_actions import create_pending_action, approve_pending_action, mark_executing, mark_executed
            action = create_pending_action(config, "calendar.cancel", "google_api", "evt789",
                                           {"reason": "cancelled", "reversible": True,
                                            "restore_hint": "update status", "provider_method": "calendar_cancel"})
            approve_pending_action(config, action["id"])
            mark_executing(config, action["id"])
            mark_executed(config, action["id"], {"success": True})
            rc = delete_actions.main(["restore", "--action-id", action["id"]])
        assert rc == 0
        google_mock.calendar_uncancel.assert_called_once_with("evt789")

    def test_restore_only_works_on_executed(self, temp_project, google_mock, auto_approve):
        """Restore should fail if action is not in 'executed' state."""
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            from pending_actions import create_pending_action
            action = create_pending_action(config, "gmail.archive", "google_api", "msg123",
                                           {"reason": "old", "reversible": True,
                                            "restore_hint": "add INBOX", "provider_method": "gmail_archive"})
            rc = delete_actions.main(["restore", "--action-id", action["id"]])
        assert rc == 1  # not executed yet
        google_mock.gmail_unarchive.assert_not_called()

    def test_restore_unknown_action_type(self, temp_project, google_mock, auto_approve):
        """Restore should fail if action type has no restore path."""
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            from pending_actions import create_pending_action, approve_pending_action, mark_executing, mark_executed
            action = create_pending_action(config, "gmail.send", "google_api", "a@b.com",
                                           {"to": "a@b.com", "subject": "S", "body": "B", "cc": ""})
            approve_pending_action(config, action["id"])
            mark_executing(config, action["id"])
            mark_executed(config, action["id"], {"success": True})
            rc = delete_actions.main(["restore", "--action-id", action["id"]])
        assert rc == 1  # gmail.send has no restore path


# ─── Provider Restore Methods ─────────────────────────────────

class TestProviderRestoreMethods:
    """Test that restore provider methods build correct commands."""

    def test_gmail_unarchive_adds_inbox(self):
        # Renamed to the provider-neutral mail_unarchive (gmail_unarchive is a
        # deprecated alias on the base class). inspect.getsource unwraps the
        # @guarded decorator to reach the real body.
        from providers.google_workspace import GoogleWorkspaceClient
        import inspect
        source = inspect.getsource(GoogleWorkspaceClient.mail_unarchive)
        assert "--add-labels" in source
        assert "INBOX" in source

    def test_gmail_untrash_removes_trash(self):
        from providers.google_workspace import GoogleWorkspaceClient
        import inspect
        source = inspect.getsource(GoogleWorkspaceClient.mail_untrash)
        assert "--remove-labels" in source
        assert "TRASH" in source

    def test_calendar_uncancel_sets_confirmed(self):
        from providers.google_workspace import GoogleWorkspaceClient
        import inspect
        source = inspect.getsource(GoogleWorkspaceClient.calendar_uncancel)
        assert "--status" in source
        assert "confirmed" in source