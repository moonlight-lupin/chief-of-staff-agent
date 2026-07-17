#!/usr/bin/env python3
"""Tests for Document Preparer handoff workflow and Meeting Prep event-based gather."""

import sys
import os
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILLS_SCRIPTS = PLUGIN_ROOT / "skills"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
for skill in ("document-preparer", "meeting-prep"):
    d = SKILLS_SCRIPTS / skill / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


@pytest.fixture
def auto_approve():
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
    yield
    os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)


@pytest.fixture
def fake_config():
    return {
        "google": {"delegate_email": "test@test.com", "account_alias": "test"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "paths": {"project_root": "/tmp/test-handoff"},
    }


@pytest.fixture
def mock_workspace():
    mock = MagicMock()
    mock.provider_name = "google_api"
    mock.supports.side_effect = lambda action: True
    # Read methods
    mock.files_search.return_value = [{"id": "f1", "name": "doc.pdf"}]
    mock.mail_search.return_value = [{"id": "m1", "subject": "Test"}]
    mock.calendar_list.return_value = []
    # Write methods return ActionResult shape
    mock.files_upload.return_value = {
        "success": True, "action": "drive.upload", "provider": "google_api",
        "tool_slug": "", "target": "/tmp/test.docx",
        "data": {"id": "f1", "webViewLink": "https://drive.google.com/file/d/f1/view"},
        "error": None, "audited": True,
    }
    mock.mail_create_draft.return_value = {
        "success": True, "action": "gmail.draft", "provider": "google_api",
        "tool_slug": "", "target": "client@test.com",
        "data": {"id": "d1"},
        "error": None, "audited": True,
    }
    return mock


class TestDocumentHandoff:
    """Document Preparer handoff: upload + draft in one command."""

    def test_handoff_calls_drive_upload_then_gmail_draft(self, fake_config, auto_approve, tmp_path):
        """Handoff under composio provider: upload + draft both called."""
        mock_composio = MagicMock()
        mock_composio.provider_name = "composio:mcp"
        mock_composio.supports.side_effect = lambda action: True  # composio supports everything
        mock_composio.files_upload.return_value = {
            "success": True, "action": "drive.upload", "provider": "composio:mcp",
            "tool_slug": "", "target": str(tmp_path / "NDA.docx"),
            "data": {"id": "f1", "webViewLink": "https://drive.google.com/file/d/f1/view"},
            "error": None, "audited": True,
        }
        mock_composio.mail_create_draft.return_value = {
            "success": True, "action": "gmail.draft", "provider": "composio:mcp",
            "tool_slug": "", "target": "client@test.com",
            "data": {"id": "d1"},
            "error": None, "audited": True,
        }
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_composio):
            import document_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = document_actions.main([
                    "handoff", "--file", str(test_file),
                    "--to", "client@test.com", "--subject", "NDA",
                    "--body", "Please review the attached NDA.",
                ])
        assert rc == 0
        mock_composio.files_upload.assert_called_once()
        mock_composio.mail_create_draft.assert_called_once()

    def test_handoff_does_not_call_gmail_send(self, fake_config, auto_approve, tmp_path):
        """Handoff under composio: gmail_send never called."""
        mock_composio = MagicMock()
        mock_composio.provider_name = "composio:mcp"
        mock_composio.supports.side_effect = lambda action: True
        mock_composio.files_upload.return_value = {
            "success": True, "data": {"webViewLink": "https://drive.google.com/file/d/f1/view"},
        }
        mock_composio.mail_create_draft.return_value = {
            "success": True, "data": {"id": "d1"},
        }
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_composio):
            import document_actions
            document_actions.main([
                "handoff", "--file", str(test_file),
                "--to", "client@test.com", "--subject", "NDA", "--body", "Body",
            ])
        mock_composio.mail_send.assert_not_called()

    def test_handoff_includes_drive_link_in_body(self, fake_config, auto_approve, tmp_path):
        """Under composio, handoff body includes the uploaded file link."""
        mock_composio = MagicMock()
        mock_composio.provider_name = "composio:mcp"
        mock_composio.supports.side_effect = lambda action: True
        mock_composio.files_upload.return_value = {
            "success": True, "data": {"webViewLink": "https://drive.google.com/file/d/f1/view"},
        }
        mock_composio.mail_create_draft.return_value = {"success": True, "data": {"id": "d1"}}
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_composio):
            import document_actions
            document_actions.main([
                "handoff", "--file", str(test_file),
                "--to", "client@test.com", "--subject", "NDA", "--body", "Please review.",
            ])
        call_args = mock_composio.mail_create_draft.call_args
        body = call_args[0][2]
        assert "https://drive.google.com/file/d/f1/view" in body
        assert "File link:" in body

    def test_handoff_fails_if_upload_fails(self, fake_config, auto_approve, tmp_path):
        """Under composio, if upload fails, draft is not called."""
        mock_composio = MagicMock()
        mock_composio.provider_name = "composio:mcp"
        mock_composio.supports.side_effect = lambda action: True
        mock_composio.files_upload.return_value = {
            "success": False, "error": "upload failed", "audited": True,
        }
        test_file = tmp_path / "bad.docx"
        test_file.write_text("dummy")
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_composio):
            import document_actions
            rc = document_actions.main([
                "handoff", "--file", str(test_file),
                "--to", "c@test.com", "--subject", "S", "--body", "B",
            ])
        assert rc == 1
        mock_composio.mail_create_draft.assert_not_called()

    def test_handoff_summary_output(self, fake_config, auto_approve, tmp_path):
        """Summary mode prints human-readable output."""
        mock_composio = MagicMock()
        mock_composio.provider_name = "composio:mcp"
        mock_composio.supports.side_effect = lambda action: True
        mock_composio.files_upload.return_value = {
            "success": True, "data": {"webViewLink": "https://drive.google.com/file/d/f1/view"},
        }
        mock_composio.mail_create_draft.return_value = {"success": True, "data": {"id": "d1"}}
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_composio):
            import document_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                document_actions.main([
                    "--summary", "handoff", "--file", str(test_file),
                    "--to", "client@test.com", "--subject", "NDA", "--body", "Body",
                ])
            output = buf.getvalue()
            assert "✅" in output or "Document handoff" in output


class TestHandoffProviderAwareness:
    """Handoff respects provider capabilities."""

    def test_handoff_google_api_succeeds_with_draft(self, fake_config, auto_approve, tmp_path):
        """Under google_api, handoff uploads then drafts when mail.draft is supported."""
        mock_google = MagicMock()
        mock_google.provider_name = "google_api"
        mock_google.supports.side_effect = lambda action: True
        mock_google.files_upload.return_value = {
            "success": True, "action": "drive.upload", "provider": "google_api",
            "data": {"id": "f1", "webViewLink": "https://drive.google.com/file/d/f1/view"},
            "audited": True,
        }
        mock_google.mail_create_draft.return_value = {
            "success": True, "action": "gmail.draft", "provider": "google_api",
            "data": {"id": "msg-1"}, "audited": True,
        }
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_google):
            import document_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = document_actions.main([
                    "handoff", "--file", str(test_file),
                    "--to", "client@test.com", "--subject", "NDA", "--body", "Body",
                ])
            data = json.loads(buf.getvalue())
        assert rc == 0
        assert data["success"] is True
        mock_google.files_upload.assert_called_once()
        mock_google.mail_create_draft.assert_called_once()

    def test_handoff_allow_partial_when_draft_capability_missing(
        self, fake_config, auto_approve, tmp_path
    ):
        """With --allow-partial, upload proceeds even if draft capability is missing."""
        mock_google = MagicMock()
        mock_google.provider_name = "google_api"
        mock_google.supports.side_effect = lambda action: action != "gmail.draft"
        mock_google.files_upload.return_value = {
            "success": True, "action": "drive.upload", "provider": "google_api",
            "data": {"id": "f1", "webViewLink": "https://drive.google.com/file/d/f1/view"},
            "audited": True,
        }
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_google):
            import document_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = document_actions.main([
                    "handoff", "--file", str(test_file),
                    "--to", "client@test.com", "--subject", "NDA", "--body", "Body",
                    "--allow-partial",
                ])
            data = json.loads(buf.getvalue())
        assert rc == 1
        assert data["success"] is False
        assert "not supported" in data["error"].lower()
        assert data["steps"]["drive_upload"] is not None
        mock_google.files_upload.assert_called_once()
        mock_google.mail_create_draft.assert_not_called()

    def test_draft_email_google_api_calls_mail_create_draft(self, fake_config, auto_approve):
        mock_google = MagicMock()
        mock_google.provider_name = "google_api"
        mock_google.supports.side_effect = lambda action: True
        mock_google.mail_create_draft.return_value = {
            "success": True, "action": "gmail.draft", "provider": "google_api",
            "data": {"id": "d1"}, "audited": True,
        }
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_google):
            import document_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = document_actions.main([
                    "draft-email", "--to", "c@test.com", "--subject", "S", "--body", "B",
                ])
            data = json.loads(buf.getvalue())
        assert rc == 0
        assert data["success"] is True
        mock_google.mail_create_draft.assert_called_once()


class TestMeetingPrepEventGather:
    """Meeting Prep gather uses event_id to find matching event."""

    def test_gather_finds_event_by_id(self, fake_config, mock_workspace):
        # Override calendar_list to return events including the target
        mock_workspace.calendar_list.return_value = [
            {"id": "evt1", "summary": "Investor Update", "start": {"dateTime": "2026-07-10T10:00:00"},
             "end": {"dateTime": "2026-07-10T11:00:00"}, "attendees": [{"email": "a@x.com"}, {"email": "b@y.com"}]},
            {"id": "evt2", "summary": "Other Meeting", "start": {}, "end": {}, "attendees": []},
        ]
        with patch("workspace_actions.load_config", return_value=fake_config), \
             patch("workspace_actions.get_client", return_value=mock_workspace):
            import workspace_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = workspace_actions.main(["gather", "--event-id", "evt1"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["event"]["id"] == "evt1"
        assert data["event"]["title"] == "Investor Update"
        assert data["event"]["attendees"] == ["a@x.com", "b@y.com"]

    def test_gather_derives_attendees_from_event(self, fake_config, mock_workspace):
        """When --attendees not passed, attendees come from the event."""
        mock_workspace.calendar_list.return_value = [
            {"id": "evt1", "summary": "Sync", "start": {"dateTime": "2026-07-10T10:00:00"},
             "end": {"dateTime": "2026-07-10T11:00:00"}, "attendees": [{"email": "person@test.com"}]},
        ]
        with patch("workspace_actions.load_config", return_value=fake_config), \
             patch("workspace_actions.get_client", return_value=mock_workspace):
            import workspace_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                workspace_actions.main(["gather", "--event-id", "evt1"])
        data = json.loads(buf.getvalue())
        assert "person@test.com" in data["event"]["attendees"]
        # Gmail should be searched for that attendee
        mock_workspace.mail_search.assert_called_with("from:person@test.com", max_results=3)

    def test_gather_uses_manual_attendees_when_passed(self, fake_config, mock_workspace):
        mock_workspace.calendar_list.return_value = [
            {"id": "evt1", "summary": "Sync", "start": {}, "end": {}, "attendees": [{"email": "internal@test.com"}]},
        ]
        with patch("workspace_actions.load_config", return_value=fake_config), \
             patch("workspace_actions.get_client", return_value=mock_workspace):
            import workspace_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                workspace_actions.main(["gather", "--event-id", "evt1", "--attendees", "external@client.com"])
        data = json.loads(buf.getvalue())
        assert "external@client.com" in data["event"]["attendees"]
        # Should search Gmail for the external attendee, not the internal one
        mock_workspace.mail_search.assert_called_with("from:external@client.com", max_results=3)

    def test_gather_uses_event_title_for_drive_search(self, fake_config, mock_workspace):
        mock_workspace.calendar_list.return_value = [
            {"id": "evt1", "summary": "Investor Update Q3", "start": {}, "end": {}, "attendees": []},
        ]
        with patch("workspace_actions.load_config", return_value=fake_config), \
             patch("workspace_actions.get_client", return_value=mock_workspace):
            import workspace_actions
            workspace_actions.main(["gather", "--event-id", "evt1"])
        mock_workspace.files_search.assert_called_with("Investor Update Q3", max_results=5)

    def test_gather_event_not_found(self, fake_config, mock_workspace):
        mock_workspace.calendar_list.return_value = [
            {"id": "other", "summary": "Other", "start": {}, "end": {}, "attendees": []},
        ]
        with patch("workspace_actions.load_config", return_value=fake_config), \
             patch("workspace_actions.get_client", return_value=mock_workspace):
            import workspace_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = workspace_actions.main(["gather", "--event-id", "missing"])
        assert rc == 1
        data = json.loads(buf.getvalue())
        assert data["event"] is None

    def test_gather_includes_recent_related_events(self, fake_config, mock_workspace):
        mock_workspace.calendar_list.return_value = [
            {"id": "evt1", "summary": "Main", "start": {}, "end": {}, "attendees": []},
            {"id": "evt2", "summary": "Related", "start": {}, "end": {}, "attendees": []},
        ]
        with patch("workspace_actions.load_config", return_value=fake_config), \
             patch("workspace_actions.get_client", return_value=mock_workspace):
            import workspace_actions
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                workspace_actions.main(["gather", "--event-id", "evt1"])
        data = json.loads(buf.getvalue())
        assert len(data["recent_related_events"]) == 1
        assert data["recent_related_events"][0]["id"] == "evt2"