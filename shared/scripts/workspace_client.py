#!/usr/bin/env python3
"""Provider-neutral workspace client for Gmail, Calendar, Drive.

Usage:
    from workspace_client import get_workspace_client
    client = get_workspace_client(config)
    emails = client.gmail_search("is:unread", max_results=10)
    events = client.calendar_list("2026-07-09", "2026-07-10")
    files = client.drive_search("name = 'NDA'", max_results=10)
"""
from __future__ import annotations

import abc
import sys
from pathlib import Path
from typing import Any, Mapping

# Ensure shared/scripts is importable
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


class WorkspaceClient(abc.ABC):
    """Abstract base for workspace providers (Gmail, Calendar, Drive)."""

    @abc.abstractmethod
    def gmail_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        """Search Gmail messages. Returns list of message dicts."""
        ...

    @abc.abstractmethod
    def calendar_list(self, start: str, end: str) -> list[dict[str, Any]]:
        """List calendar events between start and end dates (ISO format)."""
        ...

    @abc.abstractmethod
    def drive_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        """Search Drive files. Returns list of file dicts."""
        ...

    @abc.abstractmethod
    def drive_upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        """Upload a file to Drive. Returns uploaded file metadata."""
        ...

    def gmail_send(self, to: str, subject: str, body: str) -> dict[str, Any]:
        """Send an email. Providers may raise NotImplementedError."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support gmail_send")

    @abc.abstractmethod
    def health_check(self) -> bool:
        """Return True if the provider is healthy and authenticated."""
        ...


def get_workspace_client(config: Any) -> WorkspaceClient:
    """Factory: return a WorkspaceClient based on config.

    Reads config["integrations"]["workspace"]["provider"].
    Falls back to "google_api" if integrations section is missing.
    """
    integrations = config.get("integrations", {}) if isinstance(config, Mapping) else {}
    workspace_cfg = integrations.get("workspace", {}) if isinstance(integrations, Mapping) else {}
    provider = str(workspace_cfg.get("provider", "google_api") or "google_api")

    if provider == "google_api":
        from providers.google_workspace import GoogleWorkspaceClient
        return GoogleWorkspaceClient(config)
    elif provider == "composio":
        raise NotImplementedError(
            "Composio backend not yet implemented. Use google_api for now. "
            "Set integrations.workspace.provider: google_api in company.yaml."
        )
    else:
        raise ValueError(f"Unknown workspace provider: {provider}. Use 'google_api' or 'composio'.")


def _main() -> int:
    import argparse, json
    parser = argparse.ArgumentParser(description="Workspace client factory")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--status", action="store_true", help="Print provider status")
    args = parser.parse_args()

    if args.config:
        try:
            import yaml
            with open(args.config) as f:
                config = yaml.safe_load(f) or {}
        except Exception as exc:
            print(f"Error loading config: {exc}", file=sys.stderr)
            return 1
    else:
        config = {}

    if args.status:
        try:
            client = get_workspace_client(config)
            healthy = client.health_check()
            print(json.dumps({"provider": client.__class__.__name__, "healthy": healthy}))
        except NotImplementedError as exc:
            print(json.dumps({"provider": "composio", "healthy": False, "error": str(exc)}))
        except Exception as exc:
            print(json.dumps({"provider": "unknown", "healthy": False, "error": str(exc)}))
    return 0


if __name__ == "__main__":
    sys.exit(_main())