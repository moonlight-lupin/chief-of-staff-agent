#!/usr/bin/env python3
"""Composio workspace provider — mode router facade.

Routes to the appropriate backend based on config:
  mode: mcp → ComposioMCPWorkspaceClient (default, live-tested)
  mode: sdk → ComposioSDKWorkspaceClient  (legacy, requires composio SDK package)

This file re-exports helpers and the mode-appropriate client class.
The factory in workspace_client.py imports from here for backward compat.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from workspace_client import WorkspaceClient

# Re-export shared helpers from the MCP backend (canonical location)
from providers.composio_mcp_workspace import (
    load_session_meta,
    save_session_meta,
    get_enabled_tools,
)


def get_composio_client(config: Any) -> WorkspaceClient:
    """Return the right Composio client based on mode."""
    integrations = config.get("integrations", {}) if isinstance(config, dict) else {}
    workspace = integrations.get("workspace", {}) if isinstance(integrations, dict) else {}
    mode = str(workspace.get("mode", "mcp"))

    if mode == "mcp":
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(config)
    elif mode == "sdk":
        from providers.composio_sdk_workspace import ComposioSDKWorkspaceClient
        return ComposioSDKWorkspaceClient(config)
    else:
        raise ValueError(f"Unknown Composio mode: {mode}. Use 'mcp' or 'sdk'.")


# Backward compat: ComposioWorkspaceClient name routes to MCP by default
class ComposioWorkspaceClient(WorkspaceClient):
    """Facade that delegates to the mode-appropriate Composio backend."""

    def __new__(cls, config: Any) -> WorkspaceClient:
        return get_composio_client(config)