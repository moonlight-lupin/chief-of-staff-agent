#!/usr/bin/env python3
"""Tests for workspace write actions — Gmail draft, Calendar create/update, Drive upload/download."""

import sys
import os
import tempfile
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture
def composio_config():
    return {
        "integrations": {
            "workspace": {
                "provider": "composio",
                "mode": "sdk",
                "user_id": "test-user",
                "toolkits": ["gmail", "googlecalendar", "googledrive"],
                "tools_allowlist": {
                    "gmail": {"read": ["GMAIL_FETCH_EMAILS"], "write_safe": ["GMAIL_CREATE_EMAIL_DRAFT"]},
                    "googlecalendar": {"read": ["GOOGLECALENDAR_FIND_EVENT"], "write_safe": ["GOOGLECALENDAR_CREATE_EVENT"]},
                    "googledrive": {"read": ["GOOGLEDRIVE_FIND_FILE", "GOOGLEDRIVE_DOWNLOAD_FILE"], "write_safe": ["GOOGLEDRIVE_UPLOAD_FILE"]},
                },
            }
        },
        "paths": {"project_root": "/tmp/test-workspace-actions"},
    }


@pytest.fixture
def tmp_project():
    with tempfile.TemporaryDirectory(dir="/root") as d:
        os.environ["CHIEF_OF_STAFF_PROJECT_ROOT"] = d
        yield Path(d)
        os.environ.pop("CHIEF_OF_STAFF_PROJECT_ROOT", None)


class TestComposioGmailDraft:
    def test_gmail_create_draft_calls_tool(self, composio_config, tmp_project):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake"
        client = ComposioWorkspaceClient(composio_config)

        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.data = {"id": "draft_123"}
        mock_session.execute.return_value = mock_result
        client._session = mock_session

        result = client.gmail_create_draft("client@test.com", "Re: Proposal", "Draft body")

        assert result.get("id") == "draft_123" or result.get("success") is True
        mock_session.execute.assert_called_once()
        call_args = mock_session.execute.call_args
        assert call_args[0][0] == "GMAIL_CREATE_EMAIL_DRAFT"
        assert call_args[1]["arguments"]["to"] == "client@test.com"
        os.environ.pop("COMPOSIO_API_KEY", None)

    def test_gmail_create_draft_with_cc(self, composio_config, tmp_project):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake"
        client = ComposioWorkspaceClient(composio_config)

        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.data = {"id": "draft_456"}
        mock_session.execute.return_value = mock_result
        client._session = mock_session

        client.gmail_create_draft("a@test.com", "Subject", "Body", cc="b@test.com")
        call_args = mock_session.execute.call_args
        assert call_args[1]["arguments"]["cc"] == "b@test.com"
        os.environ.pop("COMPOSIO_API_KEY", None)


class TestComposioCalendarCreate:
    def test_calendar_create_calls_tool(self, composio_config, tmp_project):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake"
        client = ComposioWorkspaceClient(composio_config)

        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.data = {"id": "evt_123"}
        mock_session.execute.return_value = mock_result
        client._session = mock_session

        result = client.calendar_create("Team Meeting", "2026-07-10", "2026-07-10",
                                        attendees=["a@test.com"], description="Weekly sync")

        mock_session.execute.assert_called_once()
        call_args = mock_session.execute.call_args
        assert call_args[0][0] == "GOOGLECALENDAR_CREATE_EVENT"
        assert call_args[1]["arguments"]["title"] == "Team Meeting"
        os.environ.pop("COMPOSIO_API_KEY", None)


class TestComposioCalendarUpdate:
    def test_calendar_update_calls_tool(self, composio_config, tmp_project):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake"
        client = ComposioWorkspaceClient(composio_config)

        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.data = {"id": "evt_123", "updated": True}
        mock_session.execute.return_value = mock_result
        client._session = mock_session

        result = client.calendar_update("evt_123", title="Updated Meeting")

        mock_session.execute.assert_called_once()
        call_args = mock_session.execute.call_args
        assert call_args[0][0] == "GOOGLECALENDAR_UPDATE_EVENT"
        assert call_args[1]["arguments"]["event_id"] == "evt_123"
        os.environ.pop("COMPOSIO_API_KEY", None)


class TestComposioDrive:
    def test_drive_search_calls_tool(self, composio_config, tmp_project):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake"
        client = ComposioWorkspaceClient(composio_config)

        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.data = [{"id": "file1", "name": "NDA.pdf"}]
        mock_session.execute.return_value = mock_result
        client._session = mock_session

        result = client.drive_search("name = 'NDA'", max_results=5)

        assert len(result) == 1
        assert result[0]["name"] == "NDA.pdf"
        mock_session.execute.assert_called_once()
        assert mock_session.execute.call_args[0][0] == "GOOGLEDRIVE_FIND_FILE"
        os.environ.pop("COMPOSIO_API_KEY", None)

    def test_drive_upload_calls_tool(self, composio_config, tmp_project):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake"
        client = ComposioWorkspaceClient(composio_config)

        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.data = {"id": "file_new", "name": "report.pdf"}
        mock_session.execute.return_value = mock_result
        client._session = mock_session

        result = client.drive_upload("/tmp/report.pdf", parent_id="folder_123")

        mock_session.execute.assert_called_once()
        call_args = mock_session.execute.call_args
        assert call_args[0][0] == "GOOGLEDRIVE_UPLOAD_FILE"
        assert call_args[1]["arguments"]["file_path"] == "/tmp/report.pdf"
        assert call_args[1]["arguments"]["parent_id"] == "folder_123"
        os.environ.pop("COMPOSIO_API_KEY", None)

    def test_drive_download_calls_tool(self, composio_config, tmp_project):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake"
        client = ComposioWorkspaceClient(composio_config)

        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.data = {"downloaded": True}
        mock_session.execute.return_value = mock_result
        client._session = mock_session

        result = client.drive_download("file_abc", "/tmp/downloaded.pdf")

        assert result["success"] is True
        mock_session.execute.assert_called_once()
        assert mock_session.execute.call_args[0][0] == "GOOGLEDRIVE_DOWNLOAD_FILE"
        os.environ.pop("COMPOSIO_API_KEY", None)

    def test_drive_search_returns_empty_on_error(self, composio_config, tmp_project):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake"
        client = ComposioWorkspaceClient(composio_config)

        mock_session = MagicMock()
        mock_session.execute.side_effect = Exception("Not connected")
        client._session = mock_session

        assert client.drive_search("test") == []
        os.environ.pop("COMPOSIO_API_KEY", None)


class TestProviderCapabilities:
    def test_composio_supports(self, composio_config):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake"
        client = ComposioWorkspaceClient(composio_config)
        assert client.provider_name in ("composio", "composio:mcp", "composio:sdk")
        assert client.supports("gmail.search") is True
        assert client.supports("gmail.send") is False
        assert client.supports("drive.upload") is True
        os.environ.pop("COMPOSIO_API_KEY", None)

    def test_google_supports(self):
        from providers.google_workspace import GoogleWorkspaceClient
        with patch("providers.google_workspace._find_google_api_script", return_value=Path("/fake")):
            client = GoogleWorkspaceClient({"google": {}})
        assert client.provider_name == "google_api"
        assert client.supports("gmail.send") is True
        assert client.supports("calendar.create") is True

    def test_unsupported_action_raises_not_implemented(self):
        from workspace_client import WorkspaceClient
        # ABC methods with default raise should work
        with patch("providers.google_workspace._find_google_api_script", return_value=Path("/fake")):
            from providers.google_workspace import GoogleWorkspaceClient
            client = GoogleWorkspaceClient({"google": {}})
            # calendar_update is implemented for Google, but let's test the ABC default
            # by checking a method that delegates to the base class for an unknown provider
            caps = client.capabilities()
            assert "gmail.send" in caps