#!/usr/bin/env python3
"""Composio workspace backend for WorkspaceClient.

Implements Gmail + Calendar via Composio SDK (session.execute).
Drive methods are stubbed for v0.1.5.

Usage:
    from workspace_client import get_workspace_client
    client = get_workspace_client(config)  # config has integrations.workspace.provider: composio
    emails = client.gmail_search("is:unread", max_results=10)
"""
from __future__ import annotations

import json
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from workspace_client import WorkspaceClient

# Composio SDK import — lazy so the module loads even without composio installed
_composio = None
_Session = None


def _import_composio():
    """Lazily import Composio SDK. Returns (Composio class, None) or raises."""
    global _composio
    if _composio is not None:
        return _composio
    try:
        from composio import Composio
        _composio = Composio
        return _composio
    except ImportError as exc:
        raise ImportError(
            "composio package not installed. Install with: pip install composio-core\n"
            "Then set COMPOSIO_API_KEY in your environment."
        ) from exc


def _get_session_store_path(config: Any) -> Path:
    """Return path to .integrations/composio/session.json under project root."""
    project_root = None
    if isinstance(config, Mapping):
        paths = config.get("paths", {})
        if isinstance(paths, Mapping):
            project_root = paths.get("project_root")
    if not project_root:
        # Fallback to env
        project_root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT", str(Path.home() / ".hermes" / "projects" / "default"))
    return Path(str(project_root)).expanduser() / ".integrations" / "composio" / "session.json"


def load_session_meta(config: Any) -> dict[str, Any] | None:
    """Load saved Composio session metadata from project root."""
    path = _get_session_store_path(config)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_session_meta(config: Any, meta: dict[str, Any]) -> None:
    """Save Composio session metadata to project root."""
    path = _get_session_store_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(meta, indent=2))


def get_enabled_tools(config: Any, access_level: str = "read") -> dict[str, list[str]]:
    """Extract enabled tools from config.tools_allowlist for the given access level.

    Returns {toolkit_slug: [tool_slug, ...]} for Composio session.create(tools=...).
    """
    if not isinstance(config, Mapping):
        return {}
    integrations = config.get("integrations", {})
    if not isinstance(integrations, Mapping):
        return {}
    workspace = integrations.get("workspace", {})
    if not isinstance(workspace, Mapping):
        return {}
    allowlist = workspace.get("tools_allowlist", {})
    if not isinstance(allowlist, Mapping):
        return {}

    result: dict[str, list[str]] = {}
    for toolkit, levels in allowlist.items():
        if not isinstance(levels, Mapping):
            continue
        tools = levels.get(access_level, [])
        if isinstance(tools, list) and tools:
            result[str(toolkit)] = [str(t) for t in tools]
    return result


class ComposioWorkspaceClient(WorkspaceClient):
    """Composio SDK backend for Gmail, Calendar, Drive.

    Uses session.execute() to call Composio tools (GMAIL_FETCH_EMAILS, etc.).
    Session is created once and reused via session_id stored in project metadata.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self._validate_config()

        integrations = config.get("integrations", {}) if isinstance(config, Mapping) else {}
        workspace = integrations.get("workspace", {}) if isinstance(integrations, Mapping) else {}
        self.user_id = str(workspace.get("user_id", ""))
        self.mode = str(workspace.get("mode", "sdk"))
        self.toolkits = workspace.get("toolkits", ["gmail", "googlecalendar", "googledrive"])
        if not isinstance(self.toolkits, list):
            self.toolkits = ["gmail", "googlecalendar", "googledrive"]

        self._api_key = os.getenv("COMPOSIO_API_KEY", "")
        self._composio_instance = None
        self._session = None
        self._session_meta = load_session_meta(config)

    def _validate_config(self) -> None:
        """Check required config fields and give clear errors."""
        if not isinstance(self.config, Mapping):
            raise ValueError("Composio provider requires a config dict")
        integrations = self.config.get("integrations", {})
        if not isinstance(integrations, Mapping) or "workspace" not in integrations:
            raise ValueError("Composio provider requires integrations.workspace config section")
        workspace = integrations.get("workspace", {})
        if not isinstance(workspace, Mapping):
            raise ValueError("integrations.workspace must be a mapping")
        if not workspace.get("user_id"):
            raise ValueError(
                "Composio provider requires integrations.workspace.user_id — "
                "set it to a stable identifier for your user (e.g. 'acme-alicia')"
            )

    def _get_composio(self):
        """Get or create the Composio client instance."""
        if self._composio_instance is not None:
            return self._composio_instance
        if not self._api_key:
            raise ValueError(
                "COMPOSIO_API_KEY not set. Get one at https://dashboard.composio.dev/settings "
                "and set it in your .env file."
            )
        Composio = _import_composio()
        self._composio_instance = Composio(api_key=self._api_key)
        return self._composio_instance

    def _get_or_create_session(self):
        """Get existing session or create a new one with configured toolkits/tools."""
        if self._session is not None:
            return self._session

        composio = self._get_composio()

        # Try to reuse saved session
        if self._session_meta and self._session_meta.get("session_id"):
            try:
                self._session = composio.use(self._session_meta["session_id"])
                return self._session
            except Exception:
                # Session may have expired — create a new one
                pass

        # Create new session with restricted toolkits and tools
        read_tools = get_enabled_tools(self.config, access_level="read")
        create_kwargs: dict[str, Any] = {
            "user_id": self.user_id,
            "toolkits": list(self.toolkits),
        }
        if read_tools:
            create_kwargs["tools"] = read_tools

        self._session = composio.create(**create_kwargs)

        # Save session metadata
        meta = {
            "user_id": self.user_id,
            "session_id": getattr(self._session, "session_id", ""),
            "provider": "composio",
            "mode": self.mode,
            "toolkits": list(self.toolkits),
            "mcp": {"enabled": False, "url": None, "headers_stored": False},
            "connections": {},
        }
        for tk in self.toolkits:
            meta["connections"][tk] = {"status": "unknown", "alias": None}

        save_session_meta(self.config, meta)
        self._session_meta = meta
        return self._session

    def _check_connection(self, toolkit: str) -> bool:
        """Check if a toolkit is connected for this session."""
        session = self._get_or_create_session()
        try:
            states = session.toolkits(toolkits=[toolkit], is_connected=True)
            # If we get any results, the toolkit is connected
            return bool(states)
        except Exception:
            return False

    def gmail_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        """Search Gmail via GMAIL_FETCH_EMAILS."""
        try:
            session = self._get_or_create_session()
            result = session.execute(
                "GMAIL_FETCH_EMAILS",
                arguments={
                    "query": query,
                    "max_results": max_results,
                },
            )
            # Composio returns an object with .data or is dict-like
            data = self._extract_result(result)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "messages" in data:
                return data["messages"]
            if isinstance(data, dict):
                return [data]
            return []
        except Exception as exc:
            warnings.warn(f"Composio gmail_search failed: {exc}")
            return []

    def calendar_list(self, start: str, end: str) -> list[dict[str, Any]]:
        """List calendar events via GOOGLECALENDAR_FIND_EVENT."""
        try:
            session = self._get_or_create_session()
            result = session.execute(
                "GOOGLECALENDAR_FIND_EVENT",
                arguments={
                    "time_min": f"{start}T00:00:00Z",
                    "time_max": f"{end}T23:59:59Z",
                    "max_results": 50,
                },
            )
            data = self._extract_result(result)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "events" in data:
                return data["events"]
            if isinstance(data, dict):
                return [data]
            return []
        except Exception as exc:
            warnings.warn(f"Composio calendar_list failed: {exc}")
            return []

    def drive_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        """Search Drive — stubbed for v0.1.5."""
        warnings.warn("Composio drive_search not implemented in v0.1.5 — use google_api provider")
        return []

    def drive_upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        """Upload to Drive — stubbed for v0.1.5."""
        return {"error": "Composio drive_upload not implemented in v0.1.5", "success": False}

    def health_check(self) -> bool:
        """Check if Composio is reachable and at least one toolkit is connected."""
        try:
            session = self._get_or_create_session()
            # If session was created/reused without error, Composio is reachable
            # Check if gmail is connected
            return self._check_connection("gmail")
        except Exception:
            return False

    @staticmethod
    def _extract_result(result: Any) -> Any:
        """Extract data from Composio execute response."""
        # Composio result objects may have .data, .result, or be dict-like
        if hasattr(result, "data"):
            return result.data
        if hasattr(result, "result"):
            return result.result
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                return json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return result
        return result