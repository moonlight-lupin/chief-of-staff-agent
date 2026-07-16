#!/usr/bin/env python3
"""Tests for MCPClient — mocked HTTP, no real Composio calls."""

import sys
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture
def mcp_key():
    os.environ["COMPOSIO_MCP_KEY"] = "test-mcp-key"
    yield
    os.environ.pop("COMPOSIO_MCP_KEY", None)


class TestMCPClient:
    def test_missing_key_raises(self):
        os.environ.pop("COMPOSIO_MCP_KEY", None)
        from mcp_client import MCPClient
        client = MCPClient("https://connect.composio.dev/mcp")
        with pytest.raises(ValueError, match="COMPOSIO_MCP_KEY"):
            client._get_key()

    def test_uses_correct_key_env(self):
        os.environ["CUSTOM_MCP_KEY"] = "custom-key"
        from mcp_client import MCPClient
        client = MCPClient("https://example.com/mcp", key_env="CUSTOM_MCP_KEY")
        assert client._get_key() == "custom-key"
        os.environ.pop("CUSTOM_MCP_KEY", None)

    def test_initialize_extracts_session_id(self, mcp_key):
        from mcp_client import MCPClient
        client = MCPClient("https://connect.composio.dev/mcp")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"mcp-session-id": "test-session-123"}
        mock_response.text = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05"}}'

        with patch("mcp_client.requests.post", return_value=mock_response) as mock_post:
            result = client.initialize()

        assert client.session_id == "test-session-123"
        assert client.is_initialized is True
        # Check Bearer auth header
        call_headers = mock_post.call_args_list[0][1]["headers"]
        assert call_headers["Authorization"] == "Bearer test-mcp-key"
        payload = mock_post.call_args_list[0][1]["json"]
        assert payload["params"]["clientInfo"]["version"] == "0.3.15"

    def test_initialize_failed_http(self, mcp_key):
        from mcp_client import MCPClient
        client = MCPClient("https://connect.composio.dev/mcp")

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        with patch("mcp_client.requests.post", return_value=mock_response):
            with pytest.raises(ConnectionError, match="401"):
                client.initialize()

    def test_call_tool_parses_sse(self, mcp_key):
        from mcp_client import MCPClient
        client = MCPClient("https://connect.composio.dev/mcp")
        client._initialized = True
        client._session_id = "sess-test"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = 'event: message\ndata: {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"{\\"successful\\":true,\\"data\\":{}}"}]}}'

        with patch("mcp_client.requests.post", return_value=mock_response):
            result = client.call_tool("COMPOSIO_MULTI_EXECUTE_TOOL", {"tools": []})

        assert result.get("successful") is True

    def test_call_tool_mcp_error(self, mcp_key):
        from mcp_client import MCPClient
        client = MCPClient("https://connect.composio.dev/mcp")
        client._initialized = True
        client._session_id = "sess-test"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = 'event: message\ndata: {"jsonrpc":"2.0","error":{"code":-32602,"message":"Tool not found"}}'

        with patch("mcp_client.requests.post", return_value=mock_response):
            with pytest.raises(RuntimeError, match="Tool not found"):
                client.call_tool("UNKNOWN_TOOL", {})

    def test_call_tool_iserror_response(self, mcp_key):
        from mcp_client import MCPClient
        client = MCPClient("https://connect.composio.dev/mcp")
        client._initialized = True
        client._session_id = "sess-test"

        error_text = json.dumps({"error": "validation failed", "successful": False})
        inner_result = {"jsonrpc": "2.0", "id": 3, "result": {"isError": True, "content": [{"type": "text", "text": error_text}]}}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = f'event: message\ndata: {json.dumps(inner_result)}'

        with patch("mcp_client.requests.post", return_value=mock_response):
            result = client.call_tool("SOME_TOOL", {})

        assert result.get("successful") is False

    def test_key_not_stored_in_client(self, mcp_key):
        from mcp_client import MCPClient
        client = MCPClient("https://connect.composio.dev/mcp")
        # The key should not be stored as an attribute
        assert not hasattr(client, "_key")
        assert not hasattr(client, "api_key")