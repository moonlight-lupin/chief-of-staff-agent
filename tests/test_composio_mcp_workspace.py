#!/usr/bin/env python3
"""Tests for ComposioMCPWorkspaceClient — mocked MCP, no real calls."""

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
def mcp_config():
    return {
        "integrations": {
            "workspace": {
                "provider": "composio",
                "mode": "mcp",
                "user_id": "test-user",
                "toolkits": ["gmail", "googlecalendar", "googledrive"],
                "mcp": {
                    "endpoint": "https://connect.composio.dev/mcp",
                    "key_env": "COMPOSIO_MCP_KEY",
                },
                "tools_allowlist": {
                    "gmail": {"read": ["GMAIL_FETCH_EMAILS"], "write_safe": ["GMAIL_CREATE_EMAIL_DRAFT"]},
                    "googlecalendar": {"read": ["GOOGLECALENDAR_FIND_EVENT"], "write_safe": ["GOOGLECALENDAR_CREATE_EVENT"]},
                    "googledrive": {"read": ["GOOGLEDRIVE_FIND_FILE"], "write_safe": ["GOOGLEDRIVE_UPLOAD_FILE"]},
                },
            }
        },
        "paths": {"project_root": "/tmp/test-mcp-workspace"},
    }


@pytest.fixture
def mcp_key():
    os.environ["COMPOSIO_MCP_KEY"] = "test-key"
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
    yield
    os.environ.pop("COMPOSIO_MCP_KEY", None)
    os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)


@pytest.fixture
def tmp_project():
    with tempfile.TemporaryDirectory() as d:
        os.environ["CHIEF_OF_STAFF_PROJECT_ROOT"] = d
        yield Path(d)
        os.environ.pop("CHIEF_OF_STAFF_PROJECT_ROOT", None)


class TestComposioMCPFactory:
    def test_mcp_mode_returns_mcp_client(self, mcp_config):
        from workspace_client import get_workspace_client
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = get_workspace_client(mcp_config)
        assert isinstance(client, ComposioMCPWorkspaceClient)

    def test_provider_name_is_composio_mcp(self, mcp_config, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(mcp_config)
        assert client.provider_name == "composio:mcp"


class TestComposioMCPGmail:
    def test_gmail_search_calls_multi_execute(self, mcp_config, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(mcp_config)

        mock_mcp = MagicMock()
        mock_mcp.call_tool.return_value = {
            "data": {"results": [{"response": {"successful": True, "data": {"messages": [{"id": "m1", "subject": "Test"}]}}}]},
        }
        client._mcp_client = mock_mcp

        result = client.gmail_search("is:unread", max_results=5)

        assert len(result) == 1
        assert result[0]["subject"] == "Test"
        mock_mcp.call_tool.assert_called_once()
        call_args = mock_mcp.call_tool.call_args
        assert call_args[0][0] == "COMPOSIO_MULTI_EXECUTE_TOOL"
        tools_arg = call_args[0][1]["tools"]
        assert tools_arg[0]["tool_slug"] == "GMAIL_FETCH_EMAILS"
        assert tools_arg[0]["arguments"]["query"] == "is:unread"

    def test_gmail_create_draft_calls_multi_execute(self, mcp_config, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(mcp_config)

        mock_mcp = MagicMock()
        mock_mcp.call_tool.return_value = {
            "data": {"results": [{"response": {"successful": True, "data": {"id": "draft_123"}}}]},
        }
        client._mcp_client = mock_mcp

        result = client.gmail_create_draft("client@test.com", "Subject", "Body")

        mock_mcp.call_tool.assert_called_once()
        assert mock_mcp.call_tool.call_args[0][0] == "COMPOSIO_MULTI_EXECUTE_TOOL"
        tools_arg = mock_mcp.call_tool.call_args[0][1]["tools"]
        assert tools_arg[0]["tool_slug"] == "GMAIL_CREATE_EMAIL_DRAFT"


class TestComposioMCPCalendar:
    def test_calendar_list_calls_multi_execute(self, mcp_config, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(mcp_config)

        mock_mcp = MagicMock()
        mock_mcp.call_tool.return_value = {
            "data": {"results": [{"response": {"successful": True, "data": {"event_data": {"event_data": [{"id": "e1", "summary": "Meeting"}]}}}}]},
        }
        client._mcp_client = mock_mcp

        result = client.calendar_list("2026-07-09", "2026-07-10")

        assert len(result) == 1
        mock_mcp.call_tool.assert_called_once()
        tools_arg = mock_mcp.call_tool.call_args[0][1]["tools"]
        assert tools_arg[0]["tool_slug"] == "GOOGLECALENDAR_FIND_EVENT"

    def test_calendar_create_calls_multi_execute(self, mcp_config, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(mcp_config)

        mock_mcp = MagicMock()
        mock_mcp.call_tool.return_value = {
            "data": {"results": [{"response": {"successful": True, "data": {"id": "evt_123"}}}]},
        }
        client._mcp_client = mock_mcp

        client.calendar_create("Team Sync", "2026-07-10", "2026-07-10")

        mock_mcp.call_tool.assert_called_once()
        tools_arg = mock_mcp.call_tool.call_args[0][1]["tools"]
        assert tools_arg[0]["tool_slug"] == "GOOGLECALENDAR_CREATE_EVENT"


class TestComposioMCPDrive:
    def test_drive_search_calls_multi_execute(self, mcp_config, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(mcp_config)

        mock_mcp = MagicMock()
        mock_mcp.call_tool.return_value = {
            "data": {"results": [{"response": {"successful": True, "data": {"files": [{"id": "f1", "name": "NDA.pdf"}]}}}]},
        }
        client._mcp_client = mock_mcp

        result = client.drive_search("NDA", max_results=5)

        assert len(result) == 1
        mock_mcp.call_tool.assert_called_once()
        tools_arg = mock_mcp.call_tool.call_args[0][1]["tools"]
        assert tools_arg[0]["tool_slug"] == "GOOGLEDRIVE_FIND_FILE"

    def test_drive_upload_calls_multi_execute(self, mcp_config, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(mcp_config)

        mock_mcp = MagicMock()
        mock_mcp.call_tool.return_value = {
            "data": {"results": [{"response": {"successful": True, "data": {"id": "file_new"}}}]},
        }
        client._mcp_client = mock_mcp

        # GOOGLEDRIVE_UPLOAD_FILE takes a staged file_to_upload FileUploadable;
        # patch the MCP sandbox stager (PR #14) and assert the wired shape.
        with patch(
            "composio_files.stage_file_uploadable_via_sandbox",
            return_value={"name": "report.pdf", "mimetype": "application/pdf", "s3key": "k1"},
        ):
            client.drive_upload("/tmp/report.pdf", parent_id="folder_123")

        mock_mcp.call_tool.assert_called_once()
        tools_arg = mock_mcp.call_tool.call_args[0][1]["tools"]
        assert tools_arg[0]["tool_slug"] == "GOOGLEDRIVE_UPLOAD_FILE"
        assert tools_arg[0]["arguments"]["file_to_upload"]["s3key"] == "k1"


class TestComposioMCPMissingKey:
    def test_missing_mcp_key_raises(self, mcp_config, tmp_project):
        os.environ.pop("COMPOSIO_MCP_KEY", None)
        from providers.composio_mcp_workspace import (
            ComposioMCPWorkspaceClient, ComposioReadError,
        )
        client = ComposioMCPWorkspaceClient(mcp_config)

        mock_mcp = MagicMock()
        mock_mcp.call_tool.side_effect = ValueError("COMPOSIO_MCP_KEY not set")
        client._mcp_client = mock_mcp

        with pytest.warns(DeprecationWarning, match="gmail_search"):
            with pytest.raises(ComposioReadError, match="COMPOSIO_MCP_KEY") as ei:
                client.gmail_search("test")
        assert ei.value.operation == "mail_search"


class TestComposioMCPKeyNotStored:
    def test_key_not_in_session_metadata(self, mcp_config, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient, save_session_meta, load_session_meta
        client = ComposioMCPWorkspaceClient(mcp_config)

        # Save metadata
        save_session_meta(mcp_config, {
            "provider": "composio",
            "mode": "mcp",
            "endpoint": "https://connect.composio.dev/mcp",
            "key_env": "COMPOSIO_MCP_KEY",
            "connections": {"gmail": {"status": "connected"}},
        })

        meta = load_session_meta(mcp_config)
        # Key should NOT be in metadata
        assert "key" not in meta
        assert "api_key" not in meta
        assert meta.get("key_env") == "COMPOSIO_MCP_KEY"  # env var name is OK, actual key is NOT stored
        assert "test-key" not in str(meta)  # the actual key value must never appear


class TestValidateReadPayloadOffload:
    """Field briefing 2026-08-29: Composio tool-router offloads large
    mail_search payloads to data_preview (data=None). Name that offload
    instead of the generic malformed-response text so callers can refetch
    with leaner args.
    """

    def test_mail_search_data_preview_offload_is_named(self):
        from providers.composio_mcp_workspace import _validate_read_payload

        client = MagicMock()
        client.family = "google"
        data = {
            "data": None,
            "data_preview": {"messages": [{"id": "m1", "subject": "Offloaded"}]},
        }
        with pytest.raises(ValueError, match="offloaded the mail_search response to data_preview"):
            _validate_read_payload(client, "mail_search", "GMAIL_FETCH_EMAILS", data)
