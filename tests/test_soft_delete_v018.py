#!/usr/bin/env python3
"""Tests for v0.1.18 — soft-delete architecture, CC wiring, approved expiry.

Verifies:
- CC is passed through gmail_send provider chain
- Approved actions expire after APPROVED_EXPIRY_HOURS
- Soft-delete actions (archive, trash, cancel) go through approval queue
- No hard delete path exists
- Reason is required for delete/archive actions
- Dry-run/preflight for delete actions
- Restore/undo metadata stored in pending action
- Capability checks block composio for soft-delete actions
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
    mock.supports.side_effect = lambda action: True  # supports everything
    mock.mail_send.return_value = {
        "success": True, "action": "gmail.send", "provider": "google_api",
        "target": "a@b.com", "data": {"id": "msg123"}, "audited": True,
    }
    mock.gmail_archive.return_value = {
        "success": True, "action": "gmail.archive", "provider": "google_api",
        "target": "msg1", "data": {"reversible": True}, "audited": True,
    }
    mock.gmail_trash.return_value = {
        "success": True, "action": "gmail.trash", "provider": "google_api",
        "target": "msg1", "data": {"reversible": True}, "audited": True,
    }
    mock.drive_trash.return_value = {
        "success": True, "action": "drive.trash", "provider": "google_api",
        "target": "file1", "data": {"reversible": True}, "audited": True,
    }
    mock.calendar_cancel.return_value = {
        "success": True, "action": "calendar.cancel", "provider": "google_api",
        "target": "evt1", "data": {"reversible": True}, "audited": True,
    }
    return mock


@pytest.fixture
def composio_mock():
    mock = MagicMock()
    mock.provider_name = "composio:mcp"
    mock.supports.side_effect = lambda action: action not in (
        "gmail.send", "gmail.archive", "gmail.trash", "drive.trash", "calendar.cancel"
    )
    return mock


def _age_approved_action(config, action_id, hours_old):
    """Manually set approved_at to N hours ago."""
    from pending_actions import _load, _save
    data = _load(config)
    expected_version = data.get("_version", 0)
    data["actions"][action_id]["approved_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=hours_old)
    ).isoformat()
    _save(config, data, expected_version=expected_version)


# ─── CC Wiring ────────────────────────────────────────────────

class TestCCWiring:
    """Test that CC is passed through the entire provider chain."""

    def test_gmail_send_with_cc(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("send_email.load_config", return_value=config), \
             patch("send_email.get_client", return_value=google_mock):
            import send_email
            buf = io.StringIO()
            with redirect_stdout(buf):
                send_email.main(["prepare", "--to", "a@b.com", "--subject", "S",
                                 "--body", "B", "--cc", "boss@company.com"])
            action = json.loads(buf.getvalue())
            send_email.main(["approve", "--action-id", action["id"]])
            send_email.main(["execute", "--action-id", action["id"]])
        google_mock.mail_send.assert_called_once_with(
            to="a@b.com", subject="S", body="B", cc="boss@company.com",
        )

    def test_gmail_send_without_cc(self, temp_project, google_mock, auto_approve):
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
        google_mock.mail_send.assert_called_once_with(
            to="a@b.com", subject="S", body="B", cc=None,
        )

    def test_google_workspace_gmail_send_passes_cc(self):
        """Unit test: GoogleWorkspaceClient.gmail_send includes --cc in command."""
        from providers.google_workspace import GoogleWorkspaceClient
        client = GoogleWorkspaceClient.__new__(GoogleWorkspaceClient)
        client.config = {}
        client._provider_name = "google_api"
        client._script = Path("/fake/google_api.py")
        client._account_alias = "test"
        client._delegate_email = "test@test.com"
        # Mock _build_cmd to return a simple list, so we can see what gets extended
        with patch.object(client, "_build_cmd", side_effect=lambda *args: ["python", "google_api.py"] + list(args)) as mock_build, \
             patch.object(client, "_run", return_value=(0, '{"id":"msg1"}', '')) as mock_run, \
             patch("workspace_audit.audit_workspace_action"), \
             patch("workspace_guardrails.confirm_action", return_value=True):
            client.gmail_send("a@b.com", "Subject", "Body", cc="cc@x.com")
        # _run receives the full cmd list — check --cc was appended
        cmd = mock_run.call_args[0][0]
        assert "--cc" in cmd
        assert "cc@x.com" in cmd


# ─── Approved Execution Expiry ────────────────────────────────

class TestApprovedExpiry:
    """Test that approved actions expire if not executed in time."""

    def test_approved_action_executable_immediately(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executing, mark_executed
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        mark_executing(config, action["id"])
        result = mark_executed(config, action["id"], {"success": True})
        assert result is not None
        assert result["state"] == "executed"

    def test_approved_action_expires_after_24h(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executed
        from pending_actions import APPROVED_EXPIRY_HOURS
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        _age_approved_action(config, action["id"], APPROVED_EXPIRY_HOURS + 1)
        result = mark_executed(config, action["id"], {"success": True})
        assert result is None  # approval lapsed

    def test_lapsed_approval_marks_expired(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executing
        from pending_actions import get_pending_action, APPROVED_EXPIRY_HOURS
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        _age_approved_action(config, action["id"], APPROVED_EXPIRY_HOURS + 1)
        # mark_executing checks expiry BEFORE provider call — marks expired
        result = mark_executing(config, action["id"])
        assert result is None
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "expired"

    def test_fresh_approval_not_expired(self, temp_project):
        from pending_actions import create_pending_action, approve_pending_action, mark_executing, mark_executed
        from pending_actions import APPROVED_EXPIRY_HOURS
        config, project = temp_project
        action = create_pending_action(config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"})
        approve_pending_action(config, action["id"])
        _age_approved_action(config, action["id"], APPROVED_EXPIRY_HOURS - 1)
        mark_executing(config, action["id"])
        result = mark_executed(config, action["id"], {"success": True})
        assert result is not None


# ─── Soft-Delete Actions ──────────────────────────────────────

class TestSoftDeleteActions:
    """Test that soft-delete actions work through the approval queue."""

    def test_prepare_gmail_archive(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = delete_actions.main([
                    "prepare", "--action-type", "gmail.archive",
                    "--target", "msg123", "--reason", "Old email",
                ])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["type"] == "gmail.archive"
        assert data["state"] == "requested"

    def test_prepare_drive_trash(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            rc = delete_actions.main([
                "prepare", "--action-type", "drive.trash",
                "--target", "file123", "--reason", "Outdated document",
            ])
        assert rc == 0

    def test_prepare_calendar_cancel(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            rc = delete_actions.main([
                "prepare", "--action-type", "calendar.cancel",
                "--target", "evt123", "--reason", "Meeting cancelled",
            ])
        assert rc == 0

    def test_prepare_requires_reason(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            # argparse should reject missing --reason
            with pytest.raises(SystemExit):
                delete_actions.main([
                    "prepare", "--action-type", "gmail.archive",
                    "--target", "msg123",
                ])

    def test_prepare_blocked_for_composio(self, temp_project, composio_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=composio_mock):
            import delete_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = delete_actions.main([
                    "prepare", "--action-type", "gmail.archive",
                    "--target", "msg123", "--reason", "Test",
                ])
        assert rc == 1

    def test_execute_gmail_archive_calls_provider(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                delete_actions.main([
                    "prepare", "--action-type", "gmail.archive",
                    "--target", "msg123", "--reason", "Old",
                ])
            action = json.loads(buf.getvalue())
            delete_actions.main(["approve", "--action-id", action["id"]])
            delete_actions.main(["execute", "--action-id", action["id"]])
        google_mock.gmail_archive.assert_called_once_with("msg123")

    def test_execute_drive_trash_calls_provider(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                delete_actions.main([
                    "prepare", "--action-type", "drive.trash",
                    "--target", "file123", "--reason", "Old file",
                ])
            action = json.loads(buf.getvalue())
            delete_actions.main(["approve", "--action-id", action["id"]])
            delete_actions.main(["execute", "--action-id", action["id"]])
        google_mock.drive_trash.assert_called_once_with("file123")

    def test_execute_calendar_cancel_calls_provider(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                delete_actions.main([
                    "prepare", "--action-type", "calendar.cancel",
                    "--target", "evt123", "--reason", "Cancelled",
                ])
            action = json.loads(buf.getvalue())
            delete_actions.main(["approve", "--action-id", action["id"]])
            delete_actions.main(["execute", "--action-id", action["id"]])
        google_mock.calendar_cancel.assert_called_once_with("evt123")

    def test_dry_run_does_not_create_action(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = delete_actions.main([
                    "prepare", "--action-type", "gmail.trash",
                    "--target", "msg123", "--reason", "Spam",
                    "--dry-run",
                ])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert "dry-run" in data["action"]
        # Verify nothing was created
        from pending_actions import list_pending_actions
        actions = list_pending_actions(config, state="requested")
        assert len(actions) == 0

    def test_preflight_does_not_create_action(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            rc = delete_actions.main([
                "prepare", "--action-type", "drive.trash",
                "--target", "file123", "--reason", "Old",
                "--preflight",
            ])
        assert rc == 0
        from pending_actions import list_pending_actions
        actions = list_pending_actions(config, state="requested")
        assert len(actions) == 0


# ─── No Hard Delete Path ──────────────────────────────────────

class TestNoHardDelete:
    """Prove that no permanent delete path exists."""

    def test_no_permanent_delete_in_soft_delete_actions(self):
        """SOFT_DELETE_ACTIONS dict must not contain any 'permanent' or 'hard' actions."""
        from delete_actions import SOFT_DELETE_ACTIONS
        for action_type, meta in SOFT_DELETE_ACTIONS.items():
            assert meta["reversible"] is True, f"{action_type} must be reversible"
            assert "permanent" not in action_type.lower()
            assert "hard" not in action_type.lower()

    def test_no_permanent_flag_in_provider_methods(self):
        """Provider methods should never pass --permanent to google_api.py."""
        from providers.google_workspace import GoogleWorkspaceClient
        import inspect
        # Check that files_trash (neutral rename of drive_trash) doesn't use
        # --permanent in actual command construction.
        source = inspect.getsource(GoogleWorkspaceClient.files_trash)
        # The comment mentions "not --permanent" but the actual cmd line must not
        cmd_line = [line.strip() for line in source.splitlines()
                    if "cmd = self._build_cmd" in line or "cmd.extend" in line]
        for line in cmd_line:
            assert "--permanent" not in line, f"Command line must not contain --permanent: {line}"

    def test_delete_actions_only_supports_soft_delete_types(self):
        """The CLI should only accept known soft-delete action types."""
        from delete_actions import SOFT_DELETE_ACTIONS
        assert "gmail.archive" in SOFT_DELETE_ACTIONS
        assert "gmail.trash" in SOFT_DELETE_ACTIONS
        assert "drive.trash" in SOFT_DELETE_ACTIONS
        assert "calendar.cancel" in SOFT_DELETE_ACTIONS
        # Must NOT have hard delete
        assert "gmail.permanent_delete" not in SOFT_DELETE_ACTIONS
        assert "drive.permanent_delete" not in SOFT_DELETE_ACTIONS


# ─── Restore Metadata ─────────────────────────────────────────

class TestRestoreMetadata:
    """Test that restore/undo metadata is stored in pending actions."""

    def test_restore_hint_stored_in_payload(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                delete_actions.main([
                    "prepare", "--action-type", "gmail.trash",
                    "--target", "msg123", "--reason", "Spam",
                ])
            action = json.loads(buf.getvalue())
        assert action["payload"]["reversible"] is True
        assert "restore_hint" in action["payload"]
        assert "TRASH" in action["payload"]["restore_hint"]

    def test_preview_shows_restore_info(self, temp_project, google_mock, auto_approve):
        config, project = temp_project
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=google_mock):
            import delete_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                delete_actions.main([
                    "prepare", "--action-type", "drive.trash",
                    "--target", "file123", "--reason", "Old file",
                ])
            action = json.loads(buf.getvalue())
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                delete_actions.main(["--summary", "preview", "--action-id", action["id"]])
        out = buf2.getvalue()
        assert "Reversible:" in out
        assert "Restore:" in out


# ─── Capability Matrix ───────────────────────────────────────

class TestSoftDeleteCapabilities:
    """Test the updated capability matrix for soft-delete actions."""

    def test_google_api_supports_soft_deletes(self):
        from workspace_capabilities import supports
        assert supports("google_api", "gmail.archive") is True
        assert supports("google_api", "gmail.trash") is True
        assert supports("google_api", "drive.trash") is True
        assert supports("google_api", "calendar.cancel") is True

    def test_composio_google_soft_delete_surface(self):
        # v0.3.13: Gmail archive/trash are WIRED but not yet execution-verified
        # (the live probe rejected a draft id); they stay False until a green
        # --verify-writes re-run. Drive trash + calendar.cancel also unsupported.
        from workspace_capabilities import supports
        assert supports("composio:mcp", "gmail.archive") is False
        assert supports("composio:mcp", "gmail.trash") is False
        assert supports("composio:mcp", "drive.trash") is False
        assert supports("composio:mcp", "calendar.cancel") is False