#!/usr/bin/env python3
"""Tests for GoogleWorkspaceClient service-account delegation.

Verifies:
- _build_cmd includes --account and --as flags in the correct order
- Read methods call google_api.py with correct subcommands
- Write methods use guardrails and return ActionResult shape
- Write methods audit actions
- gmail_send is destructive (requires CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1)
"""

import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture(autouse=True)
def clean_env():
    for key in ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"):
        os.environ.pop(key, None)
    yield
    for key in ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"):
        os.environ.pop(key, None)


def make_config(account_alias="acme-advisory", delegate="alicia@acme-advisory.example",
                service_account_path="~/.hermes/secrets/acme.json"):
    return {
        "google": {
            "service_account_path": service_account_path,
            "domain": "acme-advisory.example",
            "delegate_email": delegate,
            "account_alias": account_alias,
        },
        "integrations": {"workspace": {"provider": "google_api"}},
        "paths": {"project_root": "/tmp/test-google"},
    }


@pytest.fixture
def mock_script():
    """Mock _find_google_api_script to return a fake path."""
    with patch("providers.google_workspace._find_google_api_script", return_value=Path("/fake/google_api.py")):
        yield


@pytest.fixture
def google_client(mock_script):
    from providers.google_workspace import GoogleWorkspaceClient
    return GoogleWorkspaceClient(make_config())


class TestBuildCmd:
    def test_account_alias_adds_flag(self, google_client):
        cmd = google_client._build_cmd("calendar", "list")
        assert "--account" in cmd
        idx = cmd.index("--account")
        assert cmd[idx + 1] == "acme-advisory"

    def test_delegate_email_adds_flag(self, google_client):
        cmd = google_client._build_cmd("calendar", "list")
        assert "--as" in cmd
        idx = cmd.index("--as")
        assert cmd[idx + 1] == "alicia@acme-advisory.example"

    def test_both_flags_before_command_args(self, google_client):
        cmd = google_client._build_cmd("gmail", "search", "is:unread", "--max", "5")
        # Flags should come after script path but before service args
        assert cmd[0] == sys.executable
        assert cmd[1] == "/fake/google_api.py"
        assert "--account" in cmd[2:6]  # within first few elements
        assert "--as" in cmd[2:6]
        # Service args come after
        assert "gmail" in cmd[4:]
        assert "search" in cmd[4:]

    def test_no_account_alias_omits_flag(self, mock_script):
        from providers.google_workspace import GoogleWorkspaceClient
        cfg = make_config(account_alias="")
        client = GoogleWorkspaceClient(cfg)
        cmd = client._build_cmd("calendar", "list")
        assert "--account" not in cmd

    def test_derives_account_from_service_account_path(self, mock_script):
        """If account_alias not set but service_account_path contains 'phronesis', derive it."""
        from providers.google_workspace import GoogleWorkspaceClient
        cfg = make_config(account_alias="", service_account_path="~/.hermes/phronesis_service_account.json")
        client = GoogleWorkspaceClient(cfg)
        assert client.account_alias == "phronesis"


class TestReadMethods:
    def test_gmail_search_calls_correct_command(self, google_client):
        with patch.object(google_client, "_run", return_value=(0, "[]", "")) as mock_run:
            google_client.gmail_search("is:unread", max_results=5)
        cmd = mock_run.call_args[0][0]
        assert "gmail" in cmd
        assert "search" in cmd
        assert "is:unread" in cmd
        assert "--max" in cmd
        assert "5" in cmd

    def test_calendar_list_calls_correct_command(self, google_client):
        with patch.object(google_client, "_run", return_value=(0, "[]", "")) as mock_run:
            google_client.calendar_list("2026-07-09", "2026-07-16")
        cmd = mock_run.call_args[0][0]
        assert "calendar" in cmd
        assert "list" in cmd
        assert "--start" in cmd
        # Dates should be RFC3339 formatted
        assert "2026-07-09T00:00:00Z" in cmd
        assert "--end" in cmd
        assert "2026-07-16T23:59:59Z" in cmd

    def test_drive_search_calls_correct_command(self, google_client):
        with patch.object(google_client, "_run", return_value=(0, "[]", "")) as mock_run:
            google_client.drive_search("NDA", max_results=10)
        cmd = mock_run.call_args[0][0]
        assert "drive" in cmd
        assert "search" in cmd
        assert "NDA" in cmd

    def test_health_check_calls_calendar_list(self, google_client):
        with patch.object(google_client, "_run", return_value=(0, "[]", "")) as mock_run:
            assert google_client.health_check() is True
        cmd = mock_run.call_args[0][0]
        assert "calendar" in cmd
        assert "list" in cmd


class TestWriteGuardrails:
    def test_gmail_draft_blocked_without_auto_approve(self, google_client):
        with patch(
            "providers.google_workspace._gmail_draft_via_service_account"
        ) as mock_draft:
            result = google_client.gmail_create_draft("a@b.com", "Test", "Body")
        assert result["success"] is False
        mock_draft.assert_not_called()

    def test_gmail_draft_via_sa_with_auto_approve(self, google_client):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        with patch(
            "providers.google_workspace._gmail_draft_via_service_account",
            return_value={"id": "msg-9", "draft_id": "r-9", "message_id": "msg-9"},
        ) as mock_draft:
            result = google_client.gmail_create_draft("a@b.com", "Test", "Body", cc="c@d.com")
        assert result["success"] is True
        assert result["action"] == "gmail.draft"
        assert result["audited"] is True
        assert result["data"]["id"] == "msg-9"
        kwargs = mock_draft.call_args.kwargs
        assert kwargs["to"] == "a@b.com"
        assert kwargs["cc"] == "c@d.com"
        assert "acme.json" in kwargs["service_account_path"]

    def test_calendar_create_blocked_without_auto_approve(self, google_client):
        with patch.object(google_client, "_run") as mock_run:
            result = google_client.calendar_create("Sync", "2026-07-10", "2026-07-10")
        assert result["success"] is False
        mock_run.assert_not_called()

    def test_calendar_create_proceeds_with_auto_approve(self, google_client):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        with patch.object(google_client, "_run", return_value=(0, '{"id": "e1"}', "")):
            result = google_client.calendar_create("Sync", "2026-07-10", "2026-07-10")
        assert result["success"] is True
        assert result["action"] == "calendar.create"

    def test_drive_upload_blocked_without_auto_approve(self, google_client):
        with patch.object(google_client, "_run") as mock_run:
            result = google_client.drive_upload("/tmp/test.pdf")
        assert result["success"] is False
        mock_run.assert_not_called()

    def test_drive_upload_proceeds_with_auto_approve(self, google_client):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        mock_run = MagicMock(return_value=(0, '{"id": "f1"}', ""))
        with patch.object(google_client, "_run", mock_run):
            result = google_client.drive_upload("/tmp/test.pdf", parent_id="folder1")
        assert result["success"] is True
        assert result["action"] == "drive.upload"
        # Check parent_id was passed
        cmd = mock_run.call_args[0][0]
        assert "--parent" in cmd
        assert "folder1" in cmd

    def test_drive_download_proceeds_with_auto_approve(self, google_client):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        with patch.object(google_client, "_run", return_value=(0, "", "")):
            result = google_client.drive_download("f1", "/tmp/out.pdf")
        assert result["success"] is True
        assert result["action"] == "drive.download"

    def test_calendar_update_proceeds_with_auto_approve(self, google_client):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        with patch.object(google_client, "_run", return_value=(0, '{"id": "e1"}', "")):
            result = google_client.calendar_update("e1", summary="New Title")
        assert result["success"] is True
        assert result["action"] == "calendar.update"


class TestGmailSendDestructive:
    def test_gmail_send_blocked_even_with_auto_approve(self, google_client):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        with patch.object(google_client, "_run") as mock_run:
            result = google_client.gmail_send("a@b.com", "Test", "Body")
        assert result["success"] is False
        assert "destructive" in result["error"].lower() or "guardrail" in result["error"].lower()
        mock_run.assert_not_called()

    def test_gmail_send_proceeds_with_allow_destructive(self, google_client):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = "1"
        with patch.object(google_client, "_run", return_value=(0, "sent", "")):
            result = google_client.gmail_send("a@b.com", "Test", "Body")
        assert result["success"] is True
        assert result["action"] == "gmail.send"
        assert result["audited"] is True


class TestActionResultShape:
    def test_calendar_create_returns_action_result(self, google_client):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        with patch.object(google_client, "_run", return_value=(0, '{"id": "e1"}', "")):
            result = google_client.calendar_create("Sync", "2026-07-10", "2026-07-10")
        assert "success" in result
        assert "action" in result
        assert "provider" in result
        assert "audited" in result
        assert result["provider"] == "google_api"

    def test_gmail_draft_returns_action_result(self, google_client):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        with patch(
            "providers.google_workspace._gmail_draft_via_service_account",
            return_value={"id": "m1"},
        ):
            result = google_client.gmail_create_draft("a@b.com", "Test", "Body")
        assert "success" in result
        assert "action" in result
        assert result["success"] is True
        assert result["action"] == "gmail.draft"

    def test_drive_upload_returns_action_result(self, google_client):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        with patch.object(google_client, "_run", return_value=(0, '{"id": "f1"}', "")):
            result = google_client.drive_upload("/tmp/test.pdf")
        assert "success" in result
        assert "action" in result
        assert "audited" in result


class TestAuditCalled:
    def test_calendar_create_audits(self, google_client):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        with patch.object(google_client, "_run", return_value=(0, '{"id": "e1"}', "")), \
             patch("workspace_audit.audit_workspace_action") as mock_audit:
            google_client.calendar_create("Sync", "2026-07-10", "2026-07-10")
        mock_audit.assert_called_once()
        args = mock_audit.call_args
        assert args[0][1] == "google_api"  # provider

    def test_drive_upload_audits(self, google_client):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        with patch.object(google_client, "_run", return_value=(0, '{"id": "f1"}', "")), \
             patch("workspace_audit.audit_workspace_action") as mock_audit:
            google_client.drive_upload("/tmp/test.pdf")
        mock_audit.assert_called_once()

    def test_failed_write_audits_with_status(self, google_client):
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        with patch.object(google_client, "_run", return_value=(1, "", "auth failed")), \
             patch("workspace_audit.audit_workspace_action") as mock_audit:
            google_client.calendar_create("Sync", "2026-07-10", "2026-07-10")
        mock_audit.assert_called_once()
        # Check status="failed" is in kwargs
        assert mock_audit.call_args.kwargs.get("status") == "failed"