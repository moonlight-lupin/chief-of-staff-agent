#!/usr/bin/env python3
"""Tests for v0.1.16 — approval queue and gated Gmail send.

Verifies:
- prepare creates a pending action in 'requested' state
- list shows pending actions with optional state filter
- preview shows safe view without payload execution
- approve transitions requested → approved
- cancel transitions to cancelled
- execute requires approved state, calls provider, marks executed
- execute fails if not approved
- no direct send path (must go through prepare → approve → execute)
- all state transitions are audited
- gmail.send capability check blocks composio provider
"""

import sys
import os
import json
import io
import shutil
import tempfile
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

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
    """Create a temp project root with config pointing to it."""
    project = tmp_path / "project"
    project.mkdir()
    config = {
        "google": {"delegate_email": "test@test.com", "account_alias": "test"},
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
    """Mock client that supports gmail.send (google_api)."""
    mock = MagicMock()
    mock.provider_name = "google_api"
    mock.supports.side_effect = lambda action: action != "gmail.draft"
    mock.gmail_send.return_value = {
        "success": True, "action": "gmail.send", "provider": "google_api",
        "target": "client@test.com", "data": {"id": "msg123"},
        "audited": True,
    }
    return mock


@pytest.fixture
def composio_mock():
    """Mock client that does NOT support gmail.send (composio:mcp)."""
    mock = MagicMock()
    mock.provider_name = "composio:mcp"
    mock.supports.side_effect = lambda action: action != "gmail.send"
    return mock


class TestPendingActionsStorage:
    """Unit tests for pending_actions module."""

    def test_create_pending_action(self, temp_project):
        from pending_actions import create_pending_action, get_pending_action
        config, project = temp_project
        action = create_pending_action(
            config, "gmail.send", "google_api", "client@test.com",
            {"to": "client@test.com", "subject": "Test", "body": "Hello"},
        )
        assert action["state"] == "requested"
        assert action["id"]
        # Verify it's on disk
        loaded = get_pending_action(config, action["id"])
        assert loaded is not None
        assert loaded["target"] == "client@test.com"

    def test_list_pending_actions(self, temp_project):
        from pending_actions import create_pending_action, list_pending_actions
        config, project = temp_project
        create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        create_pending_action(config, "gmail.send", "google_api", "c@d.com", {"to": "c@d.com"})
        all_actions = list_pending_actions(config)
        assert len(all_actions) == 2
        requested = list_pending_actions(config, state="requested")
        assert len(requested) == 2

    def test_approve_pending_action(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, get_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approved = approve_pending_action(config, action["id"])
        assert approved["state"] == "approved"
        assert approved["approved_at"] is not None
        # Verify on disk
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "approved"

    def test_cancel_pending_action(self, temp_project):
        from pending_actions import create_pending_action, cancel_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        cancelled = cancel_pending_action(config, action["id"])
        assert cancelled["state"] == "cancelled"
        assert cancelled["cancelled_at"] is not None

    def test_approve_not_found(self, temp_project):
        from pending_actions import approve_pending_action
        config, project = temp_project
        assert approve_pending_action(config, "nonexistent") is None

    def test_approve_already_approved_fails(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        # Second approve should fail
        assert approve_pending_action(config, action["id"]) is None

    def test_mark_executed(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executed
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        result = {"success": True, "data": {"id": "msg1"}}
        executed = mark_executed(config, action["id"], result)
        assert executed["state"] == "executed"
        assert executed["result"]["success"] is True

    def test_mark_executed_without_approval_fails(self, temp_project):
        from pending_actions import create_pending_action, mark_executed
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        result = mark_executed(config, action["id"], {"success": True})
        assert result is None  # can't execute without approval

    def test_preview_pending_action(self, temp_project):
        from pending_actions import create_pending_action, preview_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com",
                                       {"to": "a@b.com", "subject": "Test", "body": "Hello world"})
        preview = preview_pending_action(config, action["id"])
        assert preview["preview"]["to"] == "a@b.com"
        assert preview["preview"]["subject"] == "Test"
        assert preview["preview"]["body_preview"] == "Hello world"

    def test_cancel_executed_fails(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executed, cancel_pending_action
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        mark_executed(config, action["id"], {"success": True})
        assert cancel_pending_action(config, action["id"]) is None

    def test_cleanup_old_actions(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executed, cancel_pending_action, cleanup_old_actions, _load
        config, project = temp_project
        # Create and execute an old action
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        mark_executed(config, action["id"], {"success": True})
        # Manually age the executed_at timestamp
        data = _load(config)
        from datetime import datetime, timedelta
        data["actions"][action["id"]]["executed_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        from pending_actions import _save
        _save(config, data)
        # Create a fresh one
        create_pending_action(config, "gmail.send", "google_api", "c@d.com", {"to": "c@d.com"})
        removed = cleanup_old_actions(config, days=30)
        assert removed == 1


class TestSendEmailCLI:
    """Integration tests for send_email.py CLI."""

    def test_prepare_creates_pending_action(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = send_email.main(["prepare", "--to", "client@test.com", "--subject", "NDA", "--body", "Please sign."])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["state"] == "requested"
        assert data["type"] == "gmail.send"
        assert data["target"] == "client@test.com"

    def test_prepare_blocked_for_composio(self, temp_project, composio_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=composio_mock):
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = send_email.main(["prepare", "--to", "client@test.com", "--subject", "Test", "--body", "Body"])
        assert rc == 1
        data = json.loads(buf.getvalue())
        assert data["success"] is False
        assert "not supported" in data["error"].lower()

    def test_list_shows_pending_actions(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        # Prepare two actions
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            send_email.main(["prepare", "--to", "a@b.com", "--subject", "S1", "--body", "B1"])
            send_email.main(["prepare", "--to", "c@d.com", "--subject", "S2", "--body", "B2"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = send_email.main(["list"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert len(data) == 2

    def test_list_summary_mode(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            send_email.main(["prepare", "--to", "a@b.com", "--subject", "Test", "--body", "Body"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["--summary", "list"])
        out = buf.getvalue()
        assert "📨" in out
        assert "a@b.com" in out

    def test_preview_shows_safe_view(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["prepare", "--to", "client@test.com", "--subject", "NDA", "--body", "Please sign the NDA."])
            action = json.loads(buf.getvalue())
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                rc = send_email.main(["preview", "--action-id", action["id"]])
        assert rc == 0
        preview = json.loads(buf2.getvalue())
        assert preview["preview"]["to"] == "client@test.com"
        assert preview["preview"]["subject"] == "NDA"
        assert "Please sign" in preview["preview"]["body_preview"]

    def test_approve_transitions_state(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["prepare", "--to", "a@b.com", "--subject", "S", "--body", "B"])
            action = json.loads(buf.getvalue())
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                rc = send_email.main(["approve", "--action-id", action["id"]])
        assert rc == 0
        approved = json.loads(buf2.getvalue())
        assert approved["state"] == "approved"

    def test_cancel_transitions_state(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["prepare", "--to", "a@b.com", "--subject", "S", "--body", "B"])
            action = json.loads(buf.getvalue())
            rc = send_email.main(["cancel", "--action-id", action["id"]])
        assert rc == 0

    def test_execute_without_approval_fails(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["prepare", "--to", "a@b.com", "--subject", "S", "--body", "B"])
            action = json.loads(buf.getvalue())
            rc = send_email.main(["execute", "--action-id", action["id"]])
        assert rc == 1  # not approved yet

    def test_execute_with_approval_calls_provider(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["prepare", "--to", "client@test.com", "--subject", "NDA", "--body", "Sign this."])
            action = json.loads(buf.getvalue())
            send_email.main(["approve", "--action-id", action["id"]])
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                rc = send_email.main(["execute", "--action-id", action["id"]])
        assert rc == 0
        google_mock.gmail_send.assert_called_once_with(
            to="client@test.com", subject="NDA", body="Sign this.",
        )

    def test_execute_marks_action_executed(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["prepare", "--to", "client@test.com", "--subject", "NDA", "--body", "Sign this."])
            action = json.loads(buf.getvalue())
            send_email.main(["approve", "--action-id", action["id"]])
            send_email.main(["execute", "--action-id", action["id"]])
            # Verify state on disk
            from pending_actions import get_pending_action
            loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "executed"
        assert loaded["result"]["success"] is True

    def test_no_direct_send_without_prepare(self, temp_project, google_mock, auto_approve):
        """Execute with non-existent ID should fail, not send."""
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            rc = send_email.main(["execute", "--action-id", "nonexistent"])
        assert rc == 1
        google_mock.gmail_send.assert_not_called()

    def test_double_execute_fails(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["prepare", "--to", "a@b.com", "--subject", "S", "--body", "B"])
            action = json.loads(buf.getvalue())
            send_email.main(["approve", "--action-id", action["id"]])
            send_email.main(["execute", "--action-id", action["id"]])
            # Second execute should fail (already executed)
            rc = send_email.main(["execute", "--action-id", action["id"]])
        assert rc == 1
        assert google_mock.gmail_send.call_count == 1  # only sent once


class TestAuditTrail:
    """Verify all state transitions are audited."""

    def test_prepare_audits_requested(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock), \
             patch("workspace_audit.audit_workspace_action") as mock_audit:
            import send_email
            send_email.main(["prepare", "--to", "a@b.com", "--subject", "S", "--body", "B"])
        mock_audit.assert_called_once()
        assert mock_audit.call_args.kwargs.get("status") == "requested"

    def test_approve_audits_approved(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock), \
             patch("workspace_audit.audit_workspace_action") as mock_audit:
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["prepare", "--to", "a@b.com", "--subject", "S", "--body", "B"])
            action = json.loads(buf.getvalue())
            send_email.main(["approve", "--action-id", action["id"]])
        # At least 2 audit calls: prepare (requested) + approve (approved)
        statuses = [c.kwargs.get("status") for c in mock_audit.call_args_list]
        assert "requested" in statuses
        assert "approved" in statuses

    def test_cancel_audits_cancelled(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock), \
             patch("workspace_audit.audit_workspace_action") as mock_audit:
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["prepare", "--to", "a@b.com", "--subject", "S", "--body", "B"])
            action = json.loads(buf.getvalue())
            send_email.main(["cancel", "--action-id", action["id"]])
        statuses = [c.kwargs.get("status") for c in mock_audit.call_args_list]
        assert "cancelled" in statuses

    def test_execute_audits_executed(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock), \
             patch("workspace_audit.audit_workspace_action") as mock_audit:
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["prepare", "--to", "a@b.com", "--subject", "S", "--body", "B"])
            action = json.loads(buf.getvalue())
            send_email.main(["approve", "--action-id", action["id"]])
            send_email.main(["execute", "--action-id", action["id"]])
        statuses = [c.kwargs.get("status") for c in mock_audit.call_args_list]
        assert "executed" in statuses