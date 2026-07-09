#!/usr/bin/env python3
"""Tests for ComposioWorkspaceClient — all mocked, no real Composio account needed."""

import sys
import os
import tempfile
import json
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture
def composio_config():
    """Config with composio provider and user_id set."""
    return {
        "google": {
            "delegate_email": "founder@test.com",
            "account_alias": "",
        },
        "integrations": {
            "workspace": {
                "provider": "composio",
                "mode": "sdk",
                "user_id": "test-user-123",
                "toolkits": ["gmail", "googlecalendar", "googledrive"],
                "tools_allowlist": {
                    "gmail": {
                        "read": ["GMAIL_FETCH_EMAILS"],
                        "write_safe": ["GMAIL_CREATE_EMAIL_DRAFT"],
                    },
                    "googlecalendar": {
                        "read": ["GOOGLECALENDAR_FIND_EVENT"],
                        "write_safe": ["GOOGLECALENDAR_CREATE_EVENT"],
                    },
                },
            }
        },
        "paths": {
            "project_root": "/tmp/test-composio-project",
        },
    }


@pytest.fixture
def composio_config_no_user_id():
    """Config with composio provider but missing user_id."""
    return {
        "integrations": {
            "workspace": {
                "provider": "composio",
                "mode": "sdk",
            }
        }
    }


@pytest.fixture
def tmp_project_dir():
    with tempfile.TemporaryDirectory(dir="/root") as d:
        os.environ["CHIEF_OF_STAFF_PROJECT_ROOT"] = d
        yield Path(d)
        os.environ.pop("CHIEF_OF_STAFF_PROJECT_ROOT", None)


class TestComposioFactory:
    def test_factory_returns_composio_client(self, composio_config):
        from workspace_client import get_workspace_client
        from providers.composio_workspace import ComposioWorkspaceClient
        client = get_workspace_client(composio_config)
        assert isinstance(client, ComposioWorkspaceClient)

    def test_factory_raises_for_missing_user_id(self, composio_config_no_user_id):
        from workspace_client import get_workspace_client
        with pytest.raises(ValueError, match="user_id"):
            get_workspace_client(composio_config_no_user_id)


class TestComposioWorkspaceClient:
    def test_missing_api_key_raises_on_use(self, composio_config, tmp_project_dir):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ.pop("COMPOSIO_API_KEY", None)
        client = ComposioWorkspaceClient(composio_config)
        with pytest.raises(ValueError, match="COMPOSIO_API_KEY"):
            client._get_composio()

    def test_refresh_connection_statuses_updates_meta(self, composio_config, tmp_project_dir):
        from providers.composio_workspace import (
            ComposioWorkspaceClient, save_session_meta, load_session_meta
        )
        os.environ["COMPOSIO_API_KEY"] = "fake-key"

        # Save initial metadata with pending status
        save_session_meta(composio_config, {
            "user_id": "test-user-123",
            "session_id": "sess_existing",
            "connections": {"gmail": {"status": "pending"}},
        })

        client = ComposioWorkspaceClient(composio_config)

        mock_session = MagicMock()
        # First call: gmail connected, googlecalendar not
        def toolkits_mock(toolkits=None, is_connected=None):
            if toolkits and "gmail" in toolkits and is_connected:
                return [{"toolkit": "gmail"}]
            return []
        mock_session.toolkits.side_effect = toolkits_mock
        client._session = mock_session

        statuses = client.refresh_connection_statuses()
        assert statuses["gmail"] == "connected"
        assert statuses["googlecalendar"] == "pending"

        # Verify metadata was updated
        meta = load_session_meta(composio_config)
        assert meta["connections"]["gmail"]["status"] == "connected"
        assert meta["connections"]["googlecalendar"]["status"] == "pending"

        os.environ.pop("COMPOSIO_API_KEY", None)

    def test_health_check_returns_false_when_not_connected(self, composio_config, tmp_project_dir):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake-key"

        client = ComposioWorkspaceClient(composio_config)

        # Mock the session — _check_connection returns False
        mock_session = MagicMock()
        mock_session.toolkits.return_value = []  # no connections
        client._session = mock_session

        assert client.health_check() is False
        os.environ.pop("COMPOSIO_API_KEY", None)

    def test_health_check_returns_true_when_connected(self, composio_config, tmp_project_dir):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake-key"

        client = ComposioWorkspaceClient(composio_config)

        mock_session = MagicMock()
        mock_session.toolkits.return_value = [{"toolkit": "gmail", "connected": True}]
        client._session = mock_session

        assert client.health_check() is True
        os.environ.pop("COMPOSIO_API_KEY", None)

    def test_gmail_search_calls_execute(self, composio_config, tmp_project_dir):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake-key"

        client = ComposioWorkspaceClient(composio_config)

        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.data = [{"id": "msg1", "subject": "Test Email"}]
        mock_session.execute.return_value = mock_result
        client._session = mock_session

        result = client.gmail_search("is:unread", max_results=5)

        assert len(result) == 1
        assert result[0]["id"] == "msg1"
        mock_session.execute.assert_called_once()
        call_args = mock_session.execute.call_args
        assert call_args[0][0] == "GMAIL_FETCH_EMAILS"
        assert call_args[1]["arguments"]["query"] == "is:unread"

        os.environ.pop("COMPOSIO_API_KEY", None)

    def test_gmail_search_returns_empty_on_error(self, composio_config, tmp_project_dir):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake-key"

        client = ComposioWorkspaceClient(composio_config)

        mock_session = MagicMock()
        mock_session.execute.side_effect = Exception("Not connected")
        client._session = mock_session

        result = client.gmail_search("is:unread")
        assert result == []

        os.environ.pop("COMPOSIO_API_KEY", None)

    def test_calendar_list_calls_execute(self, composio_config, tmp_project_dir):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake-key"

        client = ComposioWorkspaceClient(composio_config)

        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.data = [{"id": "evt1", "title": "Meeting"}]
        mock_session.execute.return_value = mock_result
        client._session = mock_session

        result = client.calendar_list("2026-07-09", "2026-07-10")

        assert len(result) == 1
        assert result[0]["title"] == "Meeting"
        mock_session.execute.assert_called_once()
        call_args = mock_session.execute.call_args
        assert call_args[0][0] == "GOOGLECALENDAR_FIND_EVENT"

        os.environ.pop("COMPOSIO_API_KEY", None)

    def test_drive_search_stubbed(self, composio_config, tmp_project_dir):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake-key"
        client = ComposioWorkspaceClient(composio_config)
        result = client.drive_search("test")
        assert result == []
        os.environ.pop("COMPOSIO_API_KEY", None)

    def test_drive_upload_stubbed(self, composio_config, tmp_project_dir):
        from providers.composio_workspace import ComposioWorkspaceClient
        os.environ["COMPOSIO_API_KEY"] = "fake-key"
        client = ComposioWorkspaceClient(composio_config)
        result = client.drive_upload("/tmp/test.pdf")
        assert result["success"] is False
        os.environ.pop("COMPOSIO_API_KEY", None)


class TestGetEnabledTools:
    def test_returns_read_tools(self, composio_config):
        from providers.composio_workspace import get_enabled_tools
        result = get_enabled_tools(composio_config, access_level="read")
        assert "gmail" in result
        assert "GMAIL_FETCH_EMAILS" in result["gmail"]
        assert "googlecalendar" in result
        assert "GOOGLECALENDAR_FIND_EVENT" in result["googlecalendar"]

    def test_returns_write_safe_tools(self, composio_config):
        from providers.composio_workspace import get_enabled_tools
        result = get_enabled_tools(composio_config, access_level="write_safe")
        assert "gmail" in result
        assert "GMAIL_CREATE_EMAIL_DRAFT" in result["gmail"]

    def test_empty_for_unknown_level(self, composio_config):
        from providers.composio_workspace import get_enabled_tools
        result = get_enabled_tools(composio_config, access_level="admin")
        assert result == {}


class TestSessionMetadata:
    def test_save_and_load_session_meta(self, tmp_project_dir):
        from providers.composio_workspace import save_session_meta, load_session_meta
        config = {"paths": {"project_root": str(tmp_project_dir)}}
        meta = {"user_id": "test", "session_id": "sess_123", "connections": {"gmail": {"status": "connected"}}}
        save_session_meta(config, meta)

        loaded = load_session_meta(config)
        assert loaded is not None
        assert loaded["session_id"] == "sess_123"
        assert loaded["connections"]["gmail"]["status"] == "connected"
        assert "updated_at" in loaded

    def test_load_returns_none_when_missing(self, tmp_project_dir):
        from providers.composio_workspace import load_session_meta
        config = {"paths": {"project_root": str(tmp_project_dir)}}
        assert load_session_meta(config) is None