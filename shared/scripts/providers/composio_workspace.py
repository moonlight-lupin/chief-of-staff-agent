#!/usr/bin/env python3
"""Composio workspace provider — MCP backend alias.

All Composio workspace operations route through the MCP backend
(connect.composio.dev/mcp). The legacy SDK backend was removed in v0.1.9.

This file re-exports helpers and the MCP client class for backward compat.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from workspace_client import WorkspaceClient

# Re-export shared helpers from the MCP backend
from providers.composio_mcp_workspace import (
    load_session_meta,
    save_session_meta,
    get_enabled_tools,
)


def get_composio_client(config: Any) -> WorkspaceClient:
    """Return the Composio MCP client. SDK mode is no longer supported."""
    integrations = config.get("integrations", {}) if isinstance(config, dict) else {}
    workspace = integrations.get("workspace", {}) if isinstance(integrations, dict) else {}
    mode = str(workspace.get("mode", "mcp"))

    if mode == "mcp":
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(config)
    elif mode == "sdk":
        raise ValueError(
            "Composio SDK backend was removed in v0.1.9. "
            "Change mode to 'mcp' and set COMPOSIO_MCP_KEY in your .env file. "
            "See docs/SETUP.md for migration instructions."
        )
    else:
        raise ValueError(f"Unknown Composio mode: {mode}. Use 'mcp'.")


# Backward compat: ComposioWorkspaceClient name routes to MCP
class ComposioWorkspaceClient(WorkspaceClient):
    """Facade that delegates to the MCP backend."""

    def __new__(cls, config: Any) -> WorkspaceClient:
        return get_composio_client(config)