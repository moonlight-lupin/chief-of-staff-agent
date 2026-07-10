#!/usr/bin/env python3
"""Composio workspace provider — deprecated shim.

All Composio workspace operations now route directly through the MCP backend
(providers/composio_mcp_workspace.py). This file remains for backward
compatibility only. New code should import from composio_mcp_workspace.

Deprecated since v0.2.1. Will be removed in v0.3.0.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path
from typing import Any

_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from workspace_client import WorkspaceClient

# Re-export from the real backend
from providers.composio_mcp_workspace import (
    load_session_meta,
    save_session_meta,
    get_enabled_tools,
    get_composio_client,
    ComposioMCPWorkspaceClient,
)


def get_composio_client_deprecated(config: Any) -> WorkspaceClient:
    """Deprecated alias — use composio_mcp_workspace.get_composio_client."""
    warnings.warn(
        "providers.composio_workspace is deprecated. "
        "Import from providers.composio_mcp_workspace instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return get_composio_client(config)


# Backward compat: ComposioWorkspaceClient name routes to MCP
class ComposioWorkspaceClient(WorkspaceClient):
    """Deprecated facade that delegates to the MCP backend."""

    def __new__(cls, config: Any) -> WorkspaceClient:
        warnings.warn(
            "ComposioWorkspaceClient is deprecated. "
            "Use ComposioMCPWorkspaceClient directly.",
            DeprecationWarning,
            stacklevel=2,
        )
        return get_composio_client(config)