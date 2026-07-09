#!/usr/bin/env python3
"""Tests for Composio workspace provider — MCP mode only (v0.1.9+).

SDK backend was removed in v0.1.9. All tests use MCP mode with mocked MCPClient.
"""

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
    """Config with composio MCP provider."""
    return {
        "google": {"delegate_email": "founder@test.com", "account_alias": ""},
        "integrations": {
            "workspace": {
                "provider": "composio",
                "mode": "mcp",
                "user_id": "test-user-123",
                "toolkits": ["gmail", "googlecalendar", "googledrive"],
                "mcp": {
                    "endpoint": "https://connect.composio.dev/mcp",
                    "key_env": "COMPOSIO_MCP_KEY",
                },
                "tools_allowlist": {
                    "gmail": {"read": ["GMAIL_FETCH_EMAILS"], "write_safe": ["GMAIL_CREATE_EMAIL_DRAFT"]},
                    "googlecalendar": {"read": ["GOOGLECALENDAR_FIND_EVENT"], "write_safe": ["GOOGLECALENDAR_CREATE_EVENT"]},
                    "googledrive": {"read": ["GOOGLEDRIVE_FIND_FILE", "GOOGLEDRIVE_DOWNLOAD_FILE"], "write_safe": ["GOOGLEDRIVE_UPLOAD_FILE"]},
                },
            }
        },
        "paths": {"project_root": "/tmp/test-composio"},
    }


@pytest.fixture
def composio_config_no_user_id():
    return {
        "integrations": {
            "workspace": {
                "provider": "composio",
                "mode": "mcp",
                "mcp": {"endpoint": "https://connect.composio.dev/mcp", "key_env": "COMPOSIO_MCP_KEY"},
            }
        }
    }


@pytest.fixture
def composio_config_sdk_mode():
    """Config with mode: sdk — should be rejected."""
    return {
        "integrations": {
            "workspace": {
                "provider": "composio",
                "mode": "sdk",
                "user_id": "test",
            }
        }
    }


@pytest.fixture
def mcp_key():
    os.environ["COMPOSIO_MCP_KEY"] = "test-mcp-key"
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
    yield
    os.environ.pop("COMPOSIO_MCP_KEY", None)
    os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)


@pytest.fixture
def tmp_project_dir():
    with tempfile.TemporaryDirectory(dir="/root") as d:
        os.environ["CHIEF_OF_STAFF_PROJECT_ROOT"] = d
        yield Path(d)
        os.environ.pop("CHIEF_OF_STAFF_PROJECT_ROOT", None)


class TestComposioFactory:
    def test_factory_returns_mcp_client(self, composio_config, mcp_key):
        from workspace_client import get_workspace_client
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = get_workspace_client(composio_config)
        assert isinstance(client, ComposioMCPWorkspaceClient)

    def test_factory_raises_for_missing_user_id(self, composio_config_no_user_id):
        from workspace_client import get_workspace_client
        with pytest.raises(ValueError, match="user_id"):
            get_workspace_client(composio_config_no_user_id)

    def test_sdk_mode_rejected(self, composio_config_sdk_mode):
        from workspace_client import get_workspace_client
        with pytest.raises(ValueError, match="SDK backend was removed"):
            get_workspace_client(composio_config_sdk_mode)

    def test_composio_workspace_client_alias_resolves_to_mcp(self, composio_config, mcp_key):
        from providers.composio_workspace import ComposioWorkspaceClient
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioWorkspaceClient(composio_config)
        assert isinstance(client, ComposioMCPWorkspaceClient)


class TestComposioMissingKey:
    def test_missing_mcp_key_gives_clear_error(self, composio_config, tmp_project_dir):
        os.environ.pop("COMPOSIO_MCP_KEY", None)
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(composio_config)
        # Error occurs when MCP client tries to initialize (uses _get_key)
        result = client.gmail_search("test")
        assert result == []  # graceful failure, warning emitted

    def test_composio_api_key_not_required(self, composio_config, mcp_key, tmp_project_dir):
        """COMPOSIO_API_KEY should not be needed for MCP mode."""
        os.environ.pop("COMPOSIO_API_KEY", None)  # ensure it's not set
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(composio_config)
        # Should be able to get MCP client without COMPOSIO_API_KEY
        mcp = client._get_mcp()
        assert mcp is not None


class TestComposioMCPGmail:
    def test_gmail_search_calls_multi_execute(self, composio_config, mcp_key, tmp_project_dir):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(composio_config)

        mock_mcp = MagicMock()
        mock_mcp.call_tool.return_value = {
            "data": {"results": [{"response": {"successful": True, "data": {"messages": [{"id": "m1", "subject": "Test"}]}}}]},
        }
        client._mcp_client = mock_mcp

        result = client.gmail_search("is:unread", max_results=5)
        assert len(result) == 1
        mock_mcp.call_tool.assert_called_once()
        assert mock_mcp.call_tool.call_args[0][0] == "COMPOSIO_MULTI_EXECUTE_TOOL"

    def test_gmail_search_returns_empty_on_error(self, composio_config, mcp_key, tmp_project_dir):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(composio_config)

        mock_mcp = MagicMock()
        mock_mcp.call_tool.side_effect = Exception("Connection failed")
        client._mcp_client = mock_mcp

        assert client.gmail_search("is:unread") == []


class TestComposioMCPCalendar:
    def test_calendar_list_calls_multi_execute(self, composio_config, mcp_key, tmp_project_dir):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(composio_config)

        mock_mcp = MagicMock()
        mock_mcp.call_tool.return_value = {
            "data": {"results": [{"response": {"successful": True, "data": {"event_data": {"event_data": [{"id": "e1", "summary": "Meeting"}]}}}}]},
        }
        client._mcp_client = mock_mcp

        result = client.calendar_list("2026-07-09", "2026-07-10")
        assert len(result) == 1
        assert mock_mcp.call_tool.call_args[0][1]["tools"][0]["tool_slug"] == "GOOGLECALENDAR_FIND_EVENT"


class TestComposioMCPDrive:
    def test_drive_search_calls_multi_execute(self, composio_config, mcp_key, tmp_project_dir):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(composio_config)

        mock_mcp = MagicMock()
        mock_mcp.call_tool.return_value = {
            "data": {"results": [{"response": {"successful": True, "data": {"files": [{"id": "f1", "name": "NDA.pdf"}]}}}]},
        }
        client._mcp_client = mock_mcp

        result = client.drive_search("NDA", max_results=5)
        assert len(result) == 1
        assert mock_mcp.call_tool.call_args[0][1]["tools"][0]["tool_slug"] == "GOOGLEDRIVE_FIND_FILE"


class TestComposioMCPHealthCheck:
    def test_health_check_returns_false_when_not_connected(self, composio_config, mcp_key, tmp_project_dir):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(composio_config)

        mock_mcp = MagicMock()
        mock_mcp.initialize.side_effect = Exception("Connection refused")
        client._mcp_client = mock_mcp

        assert client.health_check() is False

    def test_health_check_returns_true_when_initialized(self, composio_config, mcp_key, tmp_project_dir):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(composio_config)

        mock_mcp = MagicMock()
        mock_mcp.initialize.return_value = {"result": {}}
        client._mcp_client = mock_mcp

        assert client.health_check() is True


class TestComposioMCPDraft:
    def test_gmail_create_draft_calls_multi_execute(self, composio_config, mcp_key, tmp_project_dir):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(composio_config)

        mock_mcp = MagicMock()
        mock_mcp.call_tool.return_value = {
            "data": {"results": [{"response": {"successful": True, "data": {"id": "draft_123"}}}]},
        }
        client._mcp_client = mock_mcp

        result = client.gmail_create_draft("client@test.com", "Subject", "Body")
        assert mock_mcp.call_tool.call_args[0][1]["tools"][0]["tool_slug"] == "GMAIL_CREATE_EMAIL_DRAFT"


class TestGetEnabledTools:
    def test_returns_read_tools(self, composio_config):
        from providers.composio_mcp_workspace import get_enabled_tools
        result = get_enabled_tools(composio_config, access_level="read")
        assert "GMAIL_FETCH_EMAILS" in result["gmail"]

    def test_returns_write_safe_tools(self, composio_config):
        from providers.composio_mcp_workspace import get_enabled_tools
        result = get_enabled_tools(composio_config, access_level="write_safe")
        assert "GMAIL_CREATE_EMAIL_DRAFT" in result["gmail"]


class TestSessionMetadata:
    def test_save_and_load_session_meta(self, tmp_project_dir, composio_config):
        from providers.composio_mcp_workspace import save_session_meta, load_session_meta
        config = {**composio_config, "paths": {"project_root": str(tmp_project_dir)}}
        meta = {"user_id": "test", "connections": {"gmail": {"status": "connected"}}}
        save_session_meta(config, meta)
        loaded = load_session_meta(config)
        assert loaded["connections"]["gmail"]["status"] == "connected"

    def test_mcp_key_not_stored_in_metadata(self, tmp_project_dir, composio_config, mcp_key):
        from providers.composio_mcp_workspace import save_session_meta, load_session_meta
        config = {**composio_config, "paths": {"project_root": str(tmp_project_dir)}}
        save_session_meta(config, {
            "provider": "composio",
            "mode": "mcp",
            "key_env": "COMPOSIO_MCP_KEY",
            "connections": {},
        })
        meta = load_session_meta(config)
        assert "test-mcp-key" not in str(meta)  # actual key value must never appear
        assert meta.get("key_env") == "COMPOSIO_MCP_KEY"  # env var name is OK


class TestNormalizeToolResult:
    def test_normalize_gmail(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        data = {"messages": [{"id": "1"}], "other": "stuff"}
        result = ComposioMCPWorkspaceClient._normalize_tool_result("GMAIL_FETCH_EMAILS", data)
        assert len(result) == 1
        assert result[0]["id"] == "1"

    def test_normalize_calendar(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        data = {"event_data": {"event_data": [{"id": "e1"}]}}
        result = ComposioMCPWorkspaceClient._normalize_tool_result("GOOGLECALENDAR_FIND_EVENT", data)
        assert len(result) == 1

    def test_normalize_drive(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        data = {"files": [{"id": "f1", "name": "test.pdf"}]}
        result = ComposioMCPWorkspaceClient._normalize_tool_result("GOOGLEDRIVE_FIND_FILE", data)
        assert len(result) == 1
        assert result[0]["name"] == "test.pdf"