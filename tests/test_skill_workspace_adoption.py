#!/usr/bin/env python3
"""Tests proving skills call WorkspaceClient, not provider internals.

Each skill script is tested with a mocked WorkspaceClient to verify:
- It imports get_workspace_client from workspace_client (not provider internals)
- It calls the correct WorkspaceClient methods
- Write actions return ActionResult-shaped dicts
- Read actions return lists
"""

import sys
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, ANY

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILLS_SCRIPTS = PLUGIN_ROOT / "skills"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

# Add each skill's scripts dir to path
for skill in ("calendar-manager", "drive-filer", "document-preparer", "meeting-prep", "weekly-review"):
    d = SKILLS_SCRIPTS / skill / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


@pytest.fixture
def fake_config():
    return {
        "google": {"delegate_email": "test@test.com"},
        "integrations": {
            "workspace": {
                "provider": "google_api",
            }
        },
        "paths": {"project_root": "/tmp/test-skills"},
    }


@pytest.fixture
def auto_approve():
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
    yield
    os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)


@pytest.fixture
def mock_workspace_client(fake_config):
    """Create a mock WorkspaceClient that all skill scripts will use."""
    mock = MagicMock()
    mock.provider_name = "google_api"
    mock.health_check.return_value = True
    # Read methods return lists
    mock.gmail_search.return_value = [{"id": "m1", "subject": "Test email"}]
    mock.calendar_list.return_value = [{"id": "e1", "summary": "Test event"}]
    mock.drive_search.return_value = [{"id": "f1", "name": "test.pdf"}]
    # Write methods return ActionResult-shaped dicts
    mock.gmail_create_draft.return_value = {
        "success": True, "action": "gmail.draft", "provider": "google_api",
        "tool_slug": "", "target": "test@test.com", "data": {"id": "d1"},
        "error": None, "audited": True,
    }
    mock.calendar_create.return_value = {
        "success": True, "action": "calendar.create", "provider": "google_api",
        "tool_slug": "", "target": "Test Event", "data": {"id": "e1"},
        "error": None, "audited": True,
    }
    mock.calendar_update.return_value = {
        "success": True, "action": "calendar.update", "provider": "google_api",
        "tool_slug": "", "target": "e1", "data": {}, "error": None, "audited": True,
    }
    mock.drive_upload.return_value = {
        "success": True, "action": "drive.upload", "provider": "google_api",
        "tool_slug": "", "target": "/tmp/test.pdf", "data": {"id": "f1"},
        "error": None, "audited": True,
    }
    mock.drive_download.return_value = {
        "success": True, "action": "drive.download", "provider": "google_api",
        "tool_slug": "", "target": "f1", "data": {"path": "/tmp/out.pdf"},
        "error": None, "audited": True,
    }
    return mock


class TestCalendarManager:
    """Calendar Manager uses WorkspaceClient for scan/create/update."""

    def test_scan_calls_calendar_list(self, fake_config, mock_workspace_client):
        with patch("calendar_actions.load_config", return_value=fake_config), \
             patch("calendar_actions.get_client", return_value=mock_workspace_client):
            import calendar_actions
            rc = calendar_actions.main(["scan", "--today"])
        assert rc == 0
        mock_workspace_client.calendar_list.assert_called_once()

    def test_create_calls_calendar_create(self, fake_config, mock_workspace_client, auto_approve):
        with patch("calendar_actions.load_config", return_value=fake_config), \
             patch("calendar_actions.get_client", return_value=mock_workspace_client):
            import calendar_actions
            rc = calendar_actions.main(["create", "--title", "Sync", "--start", "2026-07-10", "--end", "2026-07-10"])
        assert rc == 0
        mock_workspace_client.calendar_create.assert_called_once()
        # Check ActionResult shape in output
        mock_workspace_client.calendar_create.assert_called_with(
            title="Sync", start="2026-07-10", end="2026-07-10",
            attendees=None, description=None,
        )

    def test_update_calls_calendar_update(self, fake_config, mock_workspace_client, auto_approve):
        with patch("calendar_actions.load_config", return_value=fake_config), \
             patch("calendar_actions.get_client", return_value=mock_workspace_client):
            import calendar_actions
            rc = calendar_actions.main(["update", "--event-id", "e1", "--title", "New"])
        assert rc == 0
        mock_workspace_client.calendar_update.assert_called_once_with("e1", title="New")

    def test_does_not_import_provider_internals(self):
        """Verify calendar_actions imports from workspace_client, not providers."""
        import calendar_actions
        # The module should import get_workspace_client, not ComposioMCPWorkspaceClient etc.
        assert hasattr(calendar_actions, "get_client")
        # get_client should call workspace_client.get_workspace_client
        import inspect
        src = inspect.getsource(calendar_actions.get_client)
        assert "workspace_client" in src
        assert "get_workspace_client" in src


class TestDriveFiler:
    """Drive Filer uses WorkspaceClient for search/upload/download."""

    def test_search_calls_drive_search(self, fake_config, mock_workspace_client):
        with patch("drive_file.load_config", return_value=fake_config), \
             patch("drive_file.get_client", return_value=mock_workspace_client):
            import drive_file
            rc = drive_file.main(["search", "--query", "NDA", "--max", "5"])
        assert rc == 0
        mock_workspace_client.drive_search.assert_called_once_with("NDA", max_results=5)

    def test_upload_calls_drive_upload(self, fake_config, mock_workspace_client, auto_approve, tmp_path):
        test_file = tmp_path / "test.pdf"
        test_file.write_text("dummy")
        with patch("drive_file.load_config", return_value=fake_config), \
             patch("drive_file.get_client", return_value=mock_workspace_client):
            import drive_file
            rc = drive_file.main(["upload", "--file", str(test_file), "--parent", "folder123"])
        assert rc == 0
        mock_workspace_client.drive_upload.assert_called_once_with(str(test_file), parent_id="folder123")

    def test_download_calls_drive_download(self, fake_config, mock_workspace_client, auto_approve):
        with patch("drive_file.load_config", return_value=fake_config), \
             patch("drive_file.get_client", return_value=mock_workspace_client):
            import drive_file
            rc = drive_file.main(["download", "--file-id", "f1", "--output", "/tmp/out.pdf"])
        assert rc == 0
        mock_workspace_client.drive_download.assert_called_once_with("f1", "/tmp/out.pdf")

    def test_does_not_import_provider_internals(self):
        import drive_file
        import inspect
        src = inspect.getsource(drive_file.get_client)
        assert "workspace_client" in src
        assert "get_workspace_client" in src


class TestDocumentPreparer:
    """Document Preparer uses WorkspaceClient for upload/search/draft."""

    def test_upload_calls_drive_upload(self, fake_config, mock_workspace_client, auto_approve, tmp_path):
        test_file = tmp_path / "doc.docx"
        test_file.write_text("dummy")
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_workspace_client):
            import document_actions
            rc = document_actions.main(["upload", "--file", str(test_file), "--parent", "folder"])
        assert rc == 0
        mock_workspace_client.drive_upload.assert_called_once()

    def test_search_calls_drive_search(self, fake_config, mock_workspace_client):
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_workspace_client):
            import document_actions
            rc = document_actions.main(["search", "--query", "NDA"])
        assert rc == 0
        mock_workspace_client.drive_search.assert_called_once()

    def test_draft_email_calls_gmail_create_draft(self, fake_config, mock_workspace_client, auto_approve):
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_workspace_client):
            import document_actions
            rc = document_actions.main(["draft-email", "--to", "c@test.com", "--subject", "Test", "--body", "Body"])
        assert rc == 0
        mock_workspace_client.gmail_create_draft.assert_called_once_with(
            "c@test.com", "Test", "Body", cc=None,
        )


class TestMeetingPrep:
    """Meeting Prep uses WorkspaceClient for read-only context gathering."""

    def test_gather_calls_all_read_methods(self, fake_config, mock_workspace_client):
        with patch("workspace_actions.load_config", return_value=fake_config), \
             patch("workspace_actions.get_client", return_value=mock_workspace_client):
            import workspace_actions
            rc = workspace_actions.main([
                "gather", "--event-id", "e1",
                "--attendees", "a@x.com,b@y.com",
                "--drive-query", "meeting notes",
            ])
        assert rc == 0
        # Should call gmail_search for each attendee
        assert mock_workspace_client.gmail_search.call_count == 2
        mock_workspace_client.calendar_list.assert_called_once()
        mock_workspace_client.drive_search.assert_called_once()

    def test_gmail_context_calls_gmail_search(self, fake_config, mock_workspace_client):
        with patch("workspace_actions.load_config", return_value=fake_config), \
             patch("workspace_actions.get_client", return_value=mock_workspace_client):
            import workspace_actions
            rc = workspace_actions.main(["gmail-context", "--query", "from:a@x.com"])
        assert rc == 0
        mock_workspace_client.gmail_search.assert_called_once()

    def test_calendar_context_calls_calendar_list(self, fake_config, mock_workspace_client):
        with patch("workspace_actions.load_config", return_value=fake_config), \
             patch("workspace_actions.get_client", return_value=mock_workspace_client):
            import workspace_actions
            rc = workspace_actions.main(["calendar-context", "--start", "2026-07-09", "--end", "2026-07-16"])
        assert rc == 0
        mock_workspace_client.calendar_list.assert_called_once()

    def test_no_write_methods_called(self, fake_config, mock_workspace_client):
        """Meeting prep should never call write methods."""
        with patch("workspace_actions.load_config", return_value=fake_config), \
             patch("workspace_actions.get_client", return_value=mock_workspace_client):
            import workspace_actions
            workspace_actions.main(["gather", "--event-id", "e1"])
        mock_workspace_client.gmail_create_draft.assert_not_called()
        mock_workspace_client.calendar_create.assert_not_called()
        mock_workspace_client.calendar_update.assert_not_called()
        mock_workspace_client.drive_upload.assert_not_called()


class TestWeeklyReview:
    """Weekly Review uses WorkspaceClient for read-only data collection."""

    def test_all_calls_all_read_methods(self, fake_config, mock_workspace_client):
        with patch("workspace_collect.load_config", return_value=fake_config), \
             patch("workspace_collect.get_client", return_value=mock_workspace_client):
            import workspace_collect
            rc = workspace_collect.main(["all", "--week-start", "2026-07-06"])
        assert rc == 0
        mock_workspace_client.gmail_search.assert_called_once()
        mock_workspace_client.calendar_list.assert_called_once()
        mock_workspace_client.drive_search.assert_called_once()

    def test_gmail_calls_gmail_search(self, fake_config, mock_workspace_client):
        with patch("workspace_collect.load_config", return_value=fake_config), \
             patch("workspace_collect.get_client", return_value=mock_workspace_client):
            import workspace_collect
            rc = workspace_collect.main(["gmail"])
        assert rc == 0
        mock_workspace_client.gmail_search.assert_called_once()

    def test_calendar_calls_calendar_list(self, fake_config, mock_workspace_client):
        with patch("workspace_collect.load_config", return_value=fake_config), \
             patch("workspace_collect.get_client", return_value=mock_workspace_client):
            import workspace_collect
            rc = workspace_collect.main(["calendar", "--start", "2026-07-06", "--end", "2026-07-10"])
        assert rc == 0
        mock_workspace_client.calendar_list.assert_called_once()

    def test_no_write_methods_called(self, fake_config, mock_workspace_client):
        """Weekly review should never call write methods."""
        with patch("workspace_collect.load_config", return_value=fake_config), \
             patch("workspace_collect.get_client", return_value=mock_workspace_client):
            import workspace_collect
            workspace_collect.main(["all"])
        mock_workspace_client.gmail_create_draft.assert_not_called()
        mock_workspace_client.calendar_create.assert_not_called()
        mock_workspace_client.drive_upload.assert_not_called()


class TestActionResultShape:
    """Verify write actions return ActionResult-shaped dicts."""

    def test_calendar_create_returns_action_result(self, fake_config, mock_workspace_client, auto_approve):
        with patch("calendar_actions.load_config", return_value=fake_config), \
             patch("calendar_actions.get_client", return_value=mock_workspace_client):
            import calendar_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                calendar_actions.main(["create", "--title", "T", "--start", "2026-07-10", "--end", "2026-07-10"])
            data = json.loads(buf.getvalue())
            assert "success" in data
            assert "action" in data
            assert "provider" in data
            assert "audited" in data

    def test_drive_upload_returns_action_result(self, fake_config, mock_workspace_client, auto_approve, tmp_path):
        test_file = tmp_path / "test.pdf"
        test_file.write_text("x")
        with patch("drive_file.load_config", return_value=fake_config), \
             patch("drive_file.get_client", return_value=mock_workspace_client):
            import drive_file
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                drive_file.main(["upload", "--file", str(test_file)])
            data = json.loads(buf.getvalue())
            assert "success" in data
            assert "action" in data
            assert "audited" in data