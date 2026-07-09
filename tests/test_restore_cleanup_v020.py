#!/usr/bin/env python3
"""Tests for v0.1.20 — restore completeness, cleanup, and failed-action UX."""

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
    mock.gmail_archive.return_value = {"success": True, "action": "gmail.archive", "provider": "google_api", "data": {"reversible": True}, "audited": True}
    mock.gmail_trash.return_value = {"success": True, "action": "gmail.trash", "provider": "google_api", "data": {"reversible": True}, "audited": True}
    mock.gmail_unarchive.return_value = {"success": True, "action": "gmail.unarchive", "provider": "google_api", "data": {}, "audited": True}
    mock.gmail_untrash.return_value = {"success": True, "action": "gmail.untrash", "provider": "google_api", "data": {}, "audited": True}
    mock.calendar_cancel.return_value = {"success": True, "action": "calendar.cancel", "provider": "google_api", "data": {"reversible": True}, "audited": True}
    mock.calendar_uncancel.return_value = {"success": True, "action": "calendar.uncancel", "provider": "google_api", "data": {}, "audited": True}
    return mock


def _full_execute(config, action_id):
    """Helper: approve + mark_executing + mark_executed."""
    from pending_actions import approve_pending_action, mark_executing, mark_executed
    approve_pending_action(config, action_id)
    mark_executing(config, action_id)
    mark_executed(config, action_id, {"success": True})


# ─── Drive Restore Limitation ─────────────────────────────────

class TestDriveRestoreLimitation:
    """Drive trash has no restore path — document and verify."""

    def test_drive_trash_not_in_restore_actions(self):
        from delete_actions import RESTORE_ACTIONS
        assert "drive.trash" not in RESTORE_ACTIONS

    def test_restore_drive_trash_fails(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            from pending_actions import create_pending_action
            action = create_pending_action(config, "drive.trash", "google_api", "file123",
                                           {"reason": "old", "reversible": True,
                                            "restore_hint": "Use Drive UI", "provider_method": "drive_trash"})
            _full_execute(config, action["id"])
            rc = delete_actions.main(["restore", "--action-id", action["id"]])
        assert rc == 1


# ─── Cleanup Command ──────────────────────────────────────────

class TestCleanupCommand:
    """Test the cleanup CLI command."""

    def test_cleanup_removes_old_executed(self, temp_project, google_mock, auto_approve):
        from pending_actions import create_pending_action, _load, _save, cleanup_old_actions
        config, project = temp_project
        action = create_pending_action(config, "gmail.archive", "google_api", "msg1",
                                       {"reason": "old", "reversible": True,
                                        "restore_hint": "add INBOX", "provider_method": "gmail_archive"})
        _full_execute(config, action["id"])
        # Age the executed_at
        data = _load(config)
        ev = data.get("_version", 0)
        data["actions"][action["id"]]["executed_at"] = (
            datetime.now(timezone.utc) - timedelta(days=31)
        ).isoformat()
        _save(config, data, expected_version=ev)
        # Run cleanup
        with patch("delete_actions.load_config", return_value=config):
            import delete_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = delete_actions.main(["--summary", "cleanup", "--days", "30"])
        assert rc == 0
        assert "1 old action" in buf.getvalue()

    def test_cleanup_keeps_fresh_actions(self, temp_project, google_mock, auto_approve):
        from pending_actions import create_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.archive", "google_api", "msg1",
                                       {"reason": "old", "reversible": True,
                                        "restore_hint": "add INBOX", "provider_method": "gmail_archive"})
        _full_execute(config, action["id"])
        with patch("delete_actions.load_config", return_value=config):
            import delete_actions
            rc = delete_actions.main(["--summary", "cleanup", "--days", "30"])
        assert rc == 0
        # Action should still exist
        from pending_actions import get_pending_action
        assert get_pending_action(config, action["id"]) is not None

    def test_cleanup_json_output(self, temp_project, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config):
            import delete_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = delete_actions.main(["cleanup", "--days", "30"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert "removed" in data
        assert data["removed"] == 0


# ─── Failed Action Retry UX ───────────────────────────────────

class TestFailedActionRetryUX:
    """Test that failed actions show last_error and can be retried."""

    def test_mark_failed_stores_error(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executing, mark_failed, get_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        mark_executing(config, action["id"])
        mark_failed(config, action["id"], "Connection timeout")
        loaded = get_pending_action(config, action["id"])
        assert loaded["last_error"] == "Connection timeout"
        assert loaded["retry_count"] == 1

    def test_retry_increments_count(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executing, mark_failed, get_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        # First attempt
        mark_executing(config, action["id"])
        mark_failed(config, action["id"], "timeout 1")
        # Second attempt
        mark_executing(config, action["id"])
        mark_failed(config, action["id"], "timeout 2")
        loaded = get_pending_action(config, action["id"])
        assert loaded["retry_count"] == 2

    def test_retry_then_success(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executing, mark_failed, mark_executed, get_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        mark_executing(config, action["id"])
        mark_failed(config, action["id"], "timeout")
        mark_executing(config, action["id"])
        mark_executed(config, action["id"], {"success": True})
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "executed"
        assert loaded["retry_count"] == 1

    def test_send_email_execute_handles_exception(self, temp_project, auto_approve):
        """When provider raises exception, mark_failed is called and state is approved."""
        config, project = temp_project
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda action: True
        mock_client.gmail_send.side_effect = Exception("Network error")
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=mock_client):
            import send_email
            from pending_actions import create_pending_action, approve_pending_action, get_pending_action
            action = create_pending_action(config, "gmail.send", "google_api", "a@b.com",
                                           {"to": "a@b.com", "subject": "S", "body": "B", "cc": ""})
            approve_pending_action(config, action["id"])
            rc = send_email.main(["execute", "--action-id", action["id"]])
        assert rc == 1
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "approved"  # back to approved for retry
        assert "Network error" in loaded.get("last_error", "")

    def test_delete_execute_handles_exception(self, temp_project, auto_approve):
        """Same for delete_actions: exception → mark_failed → back to approved."""
        config, project = temp_project
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda action: True
        mock_client.gmail_archive.side_effect = Exception("API down")
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=mock_client):
            import delete_actions
            from pending_actions import create_pending_action, approve_pending_action, get_pending_action
            action = create_pending_action(config, "gmail.archive", "google_api", "msg1",
                                           {"reason": "old", "reversible": True,
                                            "restore_hint": "add INBOX", "provider_method": "gmail_archive"})
            approve_pending_action(config, action["id"])
            rc = delete_actions.main(["execute", "--action-id", action["id"]])
        assert rc == 1
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "approved"
        assert "API down" in loaded.get("last_error", "")


# ─── Restore Summary Output ───────────────────────────────────

class TestRestoreSummaryOutput:
    """Test restore summary output is operator-friendly."""

    def test_restore_summary_shows_label(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            from pending_actions import create_pending_action
            action = create_pending_action(config, "gmail.archive", "google_api", "msg1",
                                           {"reason": "old", "reversible": True,
                                            "restore_hint": "add INBOX", "provider_method": "gmail_archive"})
            _full_execute(config, action["id"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                delete_actions.main(["--summary", "restore", "--action-id", action["id"]])
        out = buf.getvalue()
        assert "Unarchive" in out
        assert "msg1" in out

    def test_restore_json_output(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            from pending_actions import create_pending_action
            action = create_pending_action(config, "gmail.trash", "google_api", "msg2",
                                           {"reason": "spam", "reversible": True,
                                            "restore_hint": "remove TRASH", "provider_method": "gmail_trash"})
            _full_execute(config, action["id"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                delete_actions.main(["restore", "--action-id", action["id"]])
        data = json.loads(buf.getvalue())
        assert data["success"] is True
        assert data["action"] == "gmail.untrash"