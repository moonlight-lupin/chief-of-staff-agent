#!/usr/bin/env python3
"""Composio MCP workspace backend.

Routes all workspace actions through Composio's MCP meta-tools:
- COMPOSIO_MANAGE_CONNECTIONS (connect toolkits)
- COMPOSIO_MULTI_EXECUTE_TOOL (execute tools by slug)

This is the default live backend for Composio (mode: mcp).
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

_SCRIPT_DIR = Path(__file__).resolve().parent.parent  # shared/scripts


def _get_session_store_path(config: Any) -> Path:
    """Return path to .integrations/composio/session.json under project root."""
    project_root = None
    if isinstance(config, Mapping):
        paths = config.get("paths", {})
        if isinstance(paths, Mapping):
            project_root = paths.get("project_root")
    if not project_root:
        project_root = os.getenv("CHIEF_OF_STAFF_PROJECT_ROOT",
                                  str(Path.home() / ".hermes" / "projects" / "default"))
    return Path(str(project_root)).expanduser() / ".integrations" / "composio" / "session.json"


def load_session_meta(config: Any) -> dict[str, Any] | None:
    path = _get_session_store_path(config)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_session_meta(config: Any, meta: dict[str, Any]) -> None:
    path = _get_session_store_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(meta, indent=2))


def get_enabled_tools(config: Any, access_level: str = "read") -> dict[str, list[str]]:
    """Extract enabled tools from config.tools_allowlist."""
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


class ComposioMCPWorkspaceClient(WorkspaceClient):
    """Composio backend using MCP meta-tools (connect.composio.dev/mcp)."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self._provider_name = "composio:mcp"
        self._validate_config()

        integrations = config.get("integrations", {}) if isinstance(config, Mapping) else {}
        workspace = integrations.get("workspace", {}) if isinstance(integrations, Mapping) else {}
        self.user_id = str(workspace.get("user_id", ""))
        self.toolkits = workspace.get("toolkits", ["gmail", "googlecalendar", "googledrive"])
        if not isinstance(self.toolkits, list):
            self.toolkits = ["gmail", "googlecalendar", "googledrive"]

        mcp_cfg = workspace.get("mcp", {}) if isinstance(workspace, Mapping) else {}
        self.endpoint = str(mcp_cfg.get("endpoint", "https://connect.composio.dev/mcp"))
        self.key_env = str(mcp_cfg.get("key_env", "COMPOSIO_MCP_KEY"))

        self._mcp_client = None
        self._session_meta = load_session_meta(config)

    def _validate_config(self) -> None:
        if not isinstance(self.config, Mapping):
            raise ValueError("Composio MCP provider requires a config dict")
        integrations = self.config.get("integrations", {})
        if not isinstance(integrations, Mapping) or "workspace" not in integrations:
            raise ValueError("Composio MCP provider requires integrations.workspace config section")
        workspace = integrations.get("workspace", {})
        if not isinstance(workspace, Mapping):
            raise ValueError("integrations.workspace must be a mapping")
        if not workspace.get("user_id"):
            raise ValueError(
                "Composio provider requires integrations.workspace.user_id — "
                "set it to a stable identifier (e.g. 'phronesis-mh')"
            )

    def _get_mcp(self):
        """Get or create the MCP client."""
        if self._mcp_client is not None:
            return self._mcp_client
        sys.path.insert(0, str(_SCRIPT_DIR))
        from mcp_client import MCPClient
        self._mcp_client = MCPClient(endpoint=self.endpoint, key_env=self.key_env)
        return self._mcp_client

    def _execute_composio_tool(self, tool_slug: str, input_data: dict[str, Any]) -> dict[str, Any]:
        """Core helper: call COMPOSIO_MULTI_EXECUTE_TOOL with a tool slug.

        Live-validated payload shape (v0.1.8):
            {"tools": [{"tool_slug": "...", "input": {...}}]}
        """
        mcp = self._get_mcp()
        result = mcp.call_tool(
            "COMPOSIO_MULTI_EXECUTE_TOOL",
            {
                "tools": [
                    {
                        "tool_slug": tool_slug,
                        "input": input_data,
                    }
                ]
            },
        )
        # Extract the actual tool response from results array
        results = result.get("data", {}).get("results", [])
        if results:
            resp = results[0].get("response", {})
            if resp.get("successful"):
                return resp.get("data", {})
            else:
                return {"error": resp.get("error", "tool execution failed"), "successful": False}
        return result

    @staticmethod
    def _normalize_tool_result(tool_slug: str, data: dict[str, Any]) -> Any:
        """Normalize live Composio response quirks into standard shapes.

        Contains all the response-shape knowledge in one place so
        workspace methods don't repeat extraction logic.
        """
        if not isinstance(data, dict):
            return data

        if tool_slug == "GMAIL_FETCH_EMAILS":
            messages = data.get("messages", [])
            return messages if isinstance(messages, list) else []

        if tool_slug == "GOOGLECALENDAR_FIND_EVENT":
            event_data = data.get("event_data", {})
            if isinstance(event_data, dict):
                events = event_data.get("event_data", [])
                return events if isinstance(events, list) else []
            return []

        if tool_slug == "GOOGLEDRIVE_FIND_FILE":
            files = data.get("files", [])
            return files if isinstance(files, list) else []

        if tool_slug == "GMAIL_CREATE_EMAIL_DRAFT":
            return data  # pass through draft metadata

        if tool_slug in ("GOOGLECALENDAR_CREATE_EVENT", "GOOGLECALENDAR_UPDATE_EVENT"):
            return data  # pass through event metadata

        if tool_slug in ("GOOGLEDRIVE_UPLOAD_FILE", "GOOGLEDRIVE_DOWNLOAD_FILE"):
            return data  # pass through file metadata

        return data

    def _manage_connections(self, action: str, toolkit: str) -> dict[str, Any]:
        """Call COMPOSIO_MANAGE_CONNECTIONS."""
        mcp = self._get_mcp()
        result = mcp.call_tool(
            "COMPOSIO_MANAGE_CONNECTIONS",
            {
                "action": action,
                "toolkits": [toolkit],
            },
        )
        return result.get("data", result)

    def refresh_connection_statuses(self) -> dict[str, str]:
        """Query Composio for actual connection state and update session metadata."""
        statuses: dict[str, str] = {}
        for toolkit in self.toolkits:
            try:
                result = self._manage_connections("status", toolkit)
                tk_info = result.get("results", {}).get(toolkit, {})
                accounts = tk_info.get("accounts", [])
                has_active = any(a.get("status") == "active" for a in accounts)
                statuses[toolkit] = "connected" if has_active else "pending"
            except Exception:
                statuses[toolkit] = "unknown"

        # Update session metadata
        meta = load_session_meta(self.config) or {}
        meta.setdefault("connections", {})
        for toolkit, status in statuses.items():
            existing = meta["connections"].get(toolkit, {})
            existing["status"] = status
            meta["connections"][toolkit] = existing
        save_session_meta(self.config, meta)
        self._session_meta = meta
        return statuses

    # --- Gmail ---

    def gmail_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        try:
            data = self._execute_composio_tool("GMAIL_FETCH_EMAILS", {
                "query": query,
                "max_results": max_results,
            })
            return self._normalize_tool_result("GMAIL_FETCH_EMAILS", data)
        except Exception as exc:
            warnings.warn(f"Composio MCP gmail_search failed: {exc}")
            return []

    def gmail_create_draft(self, to: str, subject: str, body: str,
                           cc: str | None = None) -> dict[str, Any]:
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("gmail.draft", to=to, subject=subject):
            return ActionResult(success=False, action="gmail.draft", provider=self.provider_name,
                                target=to, error="cancelled by guardrail").to_dict()
        try:
            args: dict[str, Any] = {"to": to, "subject": subject, "body": body}
            if cc:
                args["cc"] = cc
            data = self._execute_composio_tool("GMAIL_CREATE_EMAIL_DRAFT", args)
            audit_workspace_action(self.config, "composio", "gmail.create_draft",
                                   "GMAIL_CREATE_EMAIL_DRAFT", target=to)
            return ActionResult(success=True, action="gmail.draft", provider=self.provider_name,
                                tool_slug="GMAIL_CREATE_EMAIL_DRAFT", target=to,
                                data=data if isinstance(data, dict) else {}, audited=True).to_dict()
        except Exception as exc:
            audit_workspace_action(self.config, "composio", "gmail.create_draft",
                                   "GMAIL_CREATE_EMAIL_DRAFT", target=to, status="failed")
            return ActionResult(success=False, action="gmail.draft", provider=self.provider_name,
                                tool_slug="GMAIL_CREATE_EMAIL_DRAFT", target=to,
                                error=str(exc), audited=True).to_dict()

    # --- Calendar ---

    def calendar_list(self, start: str, end: str) -> list[dict[str, Any]]:
        try:
            data = self._execute_composio_tool("GOOGLECALENDAR_FIND_EVENT", {
                "time_min": f"{start}T00:00:00Z" if "T" not in start else start,
                "time_max": f"{end}T23:59:59Z" if "T" not in end else end,
                "max_results": 50,
            })
            return self._normalize_tool_result("GOOGLECALENDAR_FIND_EVENT", data)
        except Exception as exc:
            warnings.warn(f"Composio MCP calendar_list failed: {exc}")
            return []

    def calendar_create(self, title: str, start: str, end: str,
                        attendees: list[str] | None = None,
                        description: str | None = None) -> dict[str, Any]:
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("calendar.create", title=title):
            return ActionResult(success=False, action="calendar.create", provider=self.provider_name,
                                target=title, error="cancelled by guardrail").to_dict()
        try:
            args: dict[str, Any] = {
                "title": title,
                "start_time": f"{start}T00:00:00Z" if "T" not in start else start,
                "end_time": f"{end}T23:59:59Z" if "T" not in end else end,
            }
            if attendees:
                args["attendees"] = attendees
            if description:
                args["description"] = description
            data = self._execute_composio_tool("GOOGLECALENDAR_CREATE_EVENT", args)
            audit_workspace_action(self.config, "composio", "calendar.create",
                                   "GOOGLECALENDAR_CREATE_EVENT", target=title)
            return ActionResult(success=True, action="calendar.create", provider=self.provider_name,
                                tool_slug="GOOGLECALENDAR_CREATE_EVENT", target=title,
                                data=data if isinstance(data, dict) else {}, audited=True).to_dict()
        except Exception as exc:
            audit_workspace_action(self.config, "composio", "calendar.create",
                                   "GOOGLECALENDAR_CREATE_EVENT", target=title, status="failed")
            return ActionResult(success=False, action="calendar.create", provider=self.provider_name,
                                tool_slug="GOOGLECALENDAR_CREATE_EVENT", target=title,
                                error=str(exc), audited=True).to_dict()

    def calendar_update(self, event_id: str, **fields: Any) -> dict[str, Any]:
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("calendar.update", event_id=event_id):
            return ActionResult(success=False, action="calendar.update", provider=self.provider_name,
                                target=event_id, error="cancelled by guardrail").to_dict()
        try:
            args = {"event_id": event_id, **fields}
            data = self._execute_composio_tool("GOOGLECALENDAR_UPDATE_EVENT", args)
            audit_workspace_action(self.config, "composio", "calendar.update",
                                   "GOOGLECALENDAR_UPDATE_EVENT", target=event_id)
            return ActionResult(success=True, action="calendar.update", provider=self.provider_name,
                                tool_slug="GOOGLECALENDAR_UPDATE_EVENT", target=event_id,
                                data=data if isinstance(data, dict) else {}, audited=True).to_dict()
        except Exception as exc:
            audit_workspace_action(self.config, "composio", "calendar.update",
                                   "GOOGLECALENDAR_UPDATE_EVENT", target=event_id, status="failed")
            return ActionResult(success=False, action="calendar.update", provider=self.provider_name,
                                tool_slug="GOOGLECALENDAR_UPDATE_EVENT", target=event_id,
                                error=str(exc), audited=True).to_dict()

    # --- Drive ---

    def drive_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        try:
            data = self._execute_composio_tool("GOOGLEDRIVE_FIND_FILE", {
                "query": query,
                "max_results": max_results,
            })
            return self._normalize_tool_result("GOOGLEDRIVE_FIND_FILE", data)
        except Exception as exc:
            warnings.warn(f"Composio MCP drive_search failed: {exc}")
            return []

    def drive_upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("drive.upload", file=file_path):
            return ActionResult(success=False, action="drive.upload", provider=self.provider_name,
                                target=file_path, error="cancelled by guardrail").to_dict()
        try:
            args: dict[str, Any] = {"file_path": file_path}
            if parent_id:
                args["parent_id"] = parent_id
            data = self._execute_composio_tool("GOOGLEDRIVE_UPLOAD_FILE", args)
            audit_workspace_action(self.config, "composio", "drive.upload",
                                   "GOOGLEDRIVE_UPLOAD_FILE", target=file_path)
            return ActionResult(success=True, action="drive.upload", provider=self.provider_name,
                                tool_slug="GOOGLEDRIVE_UPLOAD_FILE", target=file_path,
                                data=data if isinstance(data, dict) else {}, audited=True).to_dict()
        except Exception as exc:
            audit_workspace_action(self.config, "composio", "drive.upload",
                                   "GOOGLEDRIVE_UPLOAD_FILE", target=file_path, status="failed")
            return ActionResult(success=False, action="drive.upload", provider=self.provider_name,
                                tool_slug="GOOGLEDRIVE_UPLOAD_FILE", target=file_path,
                                error=str(exc), audited=True).to_dict()

    def drive_download(self, file_id: str, output_path: str) -> dict[str, Any]:
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("drive.download", file_id=file_id):
            return ActionResult(success=False, action="drive.download", provider=self.provider_name,
                                target=file_id, error="cancelled by guardrail").to_dict()
        try:
            data = self._execute_composio_tool("GOOGLEDRIVE_DOWNLOAD_FILE", {
                "file_id": file_id,
                "output_path": output_path,
            })
            audit_workspace_action(self.config, "composio", "drive.download",
                                   "GOOGLEDRIVE_DOWNLOAD_FILE", target=file_id)
            return ActionResult(success=True, action="drive.download", provider=self.provider_name,
                                tool_slug="GOOGLEDRIVE_DOWNLOAD_FILE", target=file_id,
                                data={"path": output_path, **(data if isinstance(data, dict) else {})},
                                audited=True).to_dict()
        except Exception as exc:
            audit_workspace_action(self.config, "composio", "drive.download",
                                   "GOOGLEDRIVE_DOWNLOAD_FILE", target=file_id, status="failed")
            return ActionResult(success=False, action="drive.download", provider=self.provider_name,
                                tool_slug="GOOGLEDRIVE_DOWNLOAD_FILE", target=file_id,
                                error=str(exc), audited=True).to_dict()

    # --- Health ---

    def health_check(self) -> bool:
        try:
            mcp = self._get_mcp()
            mcp.initialize()
            return True
        except Exception:
            return False