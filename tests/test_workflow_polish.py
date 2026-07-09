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
    # Read methods
    mock.drive_search.return_value = [{"id": "f1", "name": "doc.pdf"}]
    mock.gmail_search.return_value = [{"id": "m1", "subject": "Test"}]
    mock.calendar_list.return_value = []
    # Write methods return ActionResult shape
    mock.drive_upload.return_value = {
        "success": True, "action": "drive.upload", "provider": "google_api",
        "tool_slug": "", "target": "/tmp/test.docx",
        "data": {"id": "f1", "webViewLink": "https://drive.google.com/file/d/f1/view"},
        "error": None, "audited": True,
    }
    mock.gmail_create_draft.return_value = {
        "success": True, "action": "gmail.draft", "provider": "google_api",
        "tool_slug": "", "target": "client@test.com",
        "data": {"id": "d1"},
        "error": None, "audited": True,
    }
    return mock


class TestDocumentHandoff:
    """Document Preparer handoff: upload + draft in one command."""

    def test_handoff_calls_drive_upload_then_gmail_draft(self, fake_config, mock_workspace, auto_approve, tmp_path):
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_workspace):
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
        mock_workspace.drive_upload.assert_called_once()
        mock_workspace.gmail_create_draft.assert_called_once()

    def test_handoff_does_not_call_gmail_send(self, fake_config, mock_workspace, auto_approve, tmp_path):
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_workspace):
            import document_actions
            document_actions.main([
                "handoff", "--file", str(test_file),
                "--to", "client@test.com", "--subject", "NDA", "--body", "Body",
            ])
        mock_workspace.gmail_send.assert_not_called()

    def test_handoff_includes_drive_link_in_body(self, fake_config, mock_workspace, auto_approve, tmp_path):
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_workspace):
            import document_actions
            document_actions.main([
                "handoff", "--file", str(test_file),
                "--to", "client@test.com", "--subject", "NDA", "--body", "Please review.",
            ])
        # Check the body passed to gmail_create_draft includes the Drive link
        call_args = mock_workspace.gmail_create_draft.call_args
        body = call_args[0][2]  # third positional arg = body
        assert "https://drive.google.com/file/d/f1/view" in body

    def test_handoff_fails_if_upload_fails(self, fake_config, auto_approve, tmp_path):
        test_file = tmp_path / "bad.docx"
        test_file.write_text("dummy")
        mock_fail = MagicMock()
        mock_fail.drive_upload.return_value = {
            "success": False, "action": "drive.upload", "provider": "google_api",
            "error": "upload failed", "audited": True,
        }
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_fail):
            import document_actions
            rc = document_actions.main([
                "handoff", "--file", str(test_file),
                "--to", "c@test.com", "--subject", "S", "--body", "B",
            ])
        assert rc == 1
        mock_fail.gmail_create_draft.assert_not_called()

    def test_handoff_summary_output(self, fake_config, mock_workspace, auto_approve, tmp_path):
        test_file = tmp_path / "NDA.docx"
        test_file.write_text("dummy")
        with patch("document_actions.load_config", return_value=fake_config), \
             patch("document_actions.get_client", return_value=mock_workspace):
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
        mock_workspace.gmail_search.assert_called_with("from:person@test.com", max_results=3)

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
        mock_workspace.gmail_search.assert_called_with("from:external@client.com", max_results=3)

    def test_gather_uses_event_title_for_drive_search(self, fake_config, mock_workspace):
        mock_workspace.calendar_list.return_value = [
            {"id": "evt1", "summary": "Investor Update Q3", "start": {}, "end": {}, "attendees": []},
        ]
        with patch("workspace_actions.load_config", return_value=fake_config), \
             patch("workspace_actions.get_client", return_value=mock_workspace):
            import workspace_actions
            workspace_actions.main(["gather", "--event-id", "evt1"])
        mock_workspace.drive_search.assert_called_with("Investor Update Q3", max_results=5)

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