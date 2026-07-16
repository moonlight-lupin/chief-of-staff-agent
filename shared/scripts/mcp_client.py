#!/usr/bin/env python3
"""MCP client wrapper for Composio connect.composio.dev/mcp.

Handles the JSON-RPC over SSE protocol:
1. POST initialize → get Mcp-Session-Id header
2. POST tools/call with Mcp-Session-Id header → SSE response with data: line

Uses requests (stdlib-adjacent, already installed) for HTTP.
Does NOT store the API key — reads from env var each time.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # type: ignore


class MCPClient:
    """Minimal MCP client for Composio connect.composio.dev/mcp."""

    def __init__(self, endpoint: str, key_env: str = "COMPOSIO_MCP_KEY") -> None:
        self.endpoint = endpoint
        self.key_env = key_env
        self._session_id: str | None = None
        self._initialized = False

    def _get_key(self) -> str:
        key = os.getenv(self.key_env, "")
        if not key:
            raise ValueError(
                f"{self.key_env} not set. Get a Composio MCP key and set it in your .env file."
            )
        return key

    def _headers(self) -> dict[str, str]:
        """Build headers for MCP JSON-RPC requests."""
        h: dict[str, str] = {
            "Authorization": f"Bearer {self._get_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _parse_sse(self, response_text: str) -> dict[str, Any]:
        """Parse SSE 'data: ...' lines into a single JSON object."""
        data_lines = re.findall(r"data: (.+)", response_text)
        if not data_lines:
            # Maybe plain JSON (no SSE)
            try:
                return json.loads(response_text)
            except (json.JSONDecodeError, TypeError):
                raise ValueError(f"No data in MCP response: {response_text[:200]}")
        # Join all data lines (response may span multiple)
        full = "\n".join(data_lines)
        return json.loads(full)

    def initialize(self) -> dict[str, Any]:
        """Initialize MCP session. Returns the initialize result and stores session ID."""
        if requests is None:
            raise ImportError("requests package required for MCP mode")

        payload = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "chief-of-staff", "version": "0.3.14"},
            },
            "id": 1,
        }
        r = requests.post(self.endpoint, headers=self._headers(), json=payload, timeout=30)
        if r.status_code != 200:
            raise ConnectionError(f"MCP initialize failed: HTTP {r.status_code} — {r.text[:200]}")

        # Extract session ID from response headers
        self._session_id = r.headers.get("mcp-session-id")
        if not self._session_id:
            raise ConnectionError("MCP initialize succeeded but no Mcp-Session-Id header returned")

        result = self._parse_sse(r.text)
        self._initialized = True

        # Send initialized notification (required by MCP protocol)
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        requests.post(self.endpoint, headers=self._headers(), json=notif, timeout=10)

        return result

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    def list_tools(self) -> list[dict[str, Any]]:
        """List available MCP tools."""
        self._ensure_initialized()
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {},
            "id": 2,
        }
        r = requests.post(self.endpoint, headers=self._headers(), json=payload, timeout=30)
        if r.status_code != 200:
            raise ConnectionError(f"tools/list failed: HTTP {r.status_code}")
        result = self._parse_sse(r.text)
        return result.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call an MCP tool by name with arguments. Returns parsed result."""
        self._ensure_initialized()
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
            "id": 3,
        }
        r = requests.post(self.endpoint, headers=self._headers(), json=payload, timeout=60)
        if r.status_code != 200:
            raise ConnectionError(f"tools/call '{name}' failed: HTTP {r.status_code} — {r.text[:200]}")

        result = self._parse_sse(r.text)

        # Check for MCP-level errors
        if "error" in result:
            err = result["error"]
            raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")

        # Extract content from result
        mcp_result = result.get("result", {})
        if mcp_result.get("isError"):
            # Tool returned an error — extract text
            for item in mcp_result.get("content", []):
                if item.get("type") == "text":
                    try:
                        inner = json.loads(item["text"])
                        return inner
                    except (json.JSONDecodeError, TypeError):
                        return {"error": item["text"], "successful": False}
            return {"error": "MCP tool returned isError=True", "successful": False}

        # Success — extract text content and parse
        for item in mcp_result.get("content", []):
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except (json.JSONDecodeError, TypeError):
                    return {"data": item["text"], "successful": True}

        return mcp_result

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def is_initialized(self) -> bool:
        return self._initialized