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
from workspace_guardrails import guarded
from query_compiler import compile_query

_SCRIPT_DIR = Path(__file__).resolve().parent.parent  # shared/scripts


# ── Toolkit-family architecture (v0.3.7) ──────────────────────────────────
# Composio's managed OAuth works against DIFFERENT tool catalogs depending on the
# toolkit family the operator connected. The provider is family-selectable so a
# single ComposioMCPWorkspaceClient can drive Google (Gmail/Calendar/Drive) OR
# Microsoft 365 (Outlook mail/calendar, OneDrive), chosen by config.
#
# FAMILY_SLUGS is the SINGLE source of truth mapping neutral operations to the
# default Composio tool slug per family. Every slug is OVERRIDABLE per-operation
# via integrations.workspace.tool_slugs (a flat {operation: slug} map) so a slug
# rename in Composio's catalog is a config edit, not a code change.
FAMILY_SLUGS: dict[str, dict[str, str]] = {
    "google": {
        "mail_search": "GMAIL_FETCH_EMAILS",
        "mail_create_draft": "GMAIL_CREATE_EMAIL_DRAFT",
        "calendar_list": "GOOGLECALENDAR_FIND_EVENT",
        "calendar_create": "GOOGLECALENDAR_CREATE_EVENT",
        "calendar_update": "GOOGLECALENDAR_UPDATE_EVENT",
        "files_search": "GOOGLEDRIVE_FIND_FILE",
        "files_upload": "GOOGLEDRIVE_UPLOAD_FILE",
        "files_download": "GOOGLEDRIVE_DOWNLOAD_FILE",
    },
    # Microsoft (Outlook + OneDrive) slugs corrected against Composio's LIVE
    # catalog (v0.3.7 acceptance test, 2026-07). The reads (mail_search,
    # calendar_list) were execution-verified against an active Outlook connection;
    # the writes and files_search were confirmed to exist in the catalog via
    # COMPOSIO_SEARCH_TOOLS / COMPOSIO_GET_TOOL_SCHEMAS. The earlier defaults used
    # a doubled toolkit prefix (OUTLOOK_OUTLOOK_*, ONE_DRIVE_ONE_DRIVE_*) that no
    # real slug carries. Any slug is still overridable per-operation via
    # integrations.workspace.tool_slugs without a code change.
    "microsoft": {
        "mail_search": "OUTLOOK_QUERY_EMAILS",
        "mail_create_draft": "OUTLOOK_CREATE_DRAFT",
        "calendar_list": "OUTLOOK_GET_CALENDAR_VIEW",
        "calendar_create": "OUTLOOK_CALENDAR_CREATE_EVENT",
        "calendar_update": "OUTLOOK_UPDATE_CALENDAR_EVENT",
        "files_search": "ONE_DRIVE_SEARCH_ITEMS",
        "files_upload": "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE",
        "files_download": "ONE_DRIVE_DOWNLOAD_FILE",
    },
}

# Toolkit names that imply the microsoft family when family is not set explicitly.
_MICROSOFT_TOOLKITS = {"outlook", "one_drive", "onedrive"}

VALID_FAMILIES = ("google", "microsoft")


class ComposioToolError(RuntimeError):
    """Raised when Composio reports an unknown/invalid tool slug for an operation.

    The message names the failing slug AND the tool_slugs config override path so
    a catalog drift is self-diagnosing (reads warn with it; guarded writes surface
    it as the ActionResult error).
    """


class ComposioConnectionError(RuntimeError):
    """Raised when Composio reports the toolkit has no active connection.

    A missing connection is a HARD, persistent operator problem (unlike a
    transient rate-limit), so it must NOT be swallowed into an empty successful
    read — that would falsely certify a capability the mailbox/drive cannot serve.
    Reads warn with it (so verification honestly fails the check); guarded writes
    surface it as the ActionResult error.
    """


def _is_unknown_tool_error(err: Any) -> bool:
    """True if a Composio error payload/string looks like an unknown-tool error."""
    text = str(err).lower()
    needles = (
        "unknown tool", "no such tool", "invalid tool", "tool not found",
        "not found", "does not exist", "no tool", "unrecognized tool",
        "not a valid tool", "invalid tool_slug", "unknown slug",
    )
    return any(n in text for n in needles)


def _is_connection_error(err: Any) -> bool:
    """True if a Composio error payload/string looks like a no-active-connection
    error (the toolkit was never connected / the OAuth link is still pending)."""
    text = str(err).lower()
    needles = (
        "no active connection", "no connection found", "connection not found",
        "not connected", "establish a connection", "no connected account",
    )
    return any(n in text for n in needles)


def _connection_error_message(operation: str, slug: str, err: Any) -> str:
    op = operation or "<operation>"
    return (
        f"Composio reports no active connection for the toolkit backing slug "
        f"{slug!r} (operation {op!r}): {err}. Connect the toolkit "
        f"(connect_workspace.py --provider composio --connect <toolkit>) and wait "
        f"for it to become active, then retry."
    )


def _unknown_tool_message(operation: str, slug: str, err: Any) -> str:
    op = operation or "<operation>"
    return (
        f"Composio returned an unknown-tool error for slug {slug!r} "
        f"(operation {op!r}): {err}. The default slug may be wrong for your "
        f"toolkit family — override it via integrations.workspace.tool_slugs."
        f"{op} in company.yaml."
    )


# ── Microsoft (Graph-shaped) normalizers ──────────────────────────────────
# Composio surfaces Outlook/OneDrive payloads in Microsoft Graph shape. These
# small local functions MIRROR providers/m365_graph.py's normalizers (rather than
# importing that module, which would couple this provider to the Graph REST
# client and its requests/query_compiler surface) so a Composio-Microsoft record
# validates against the SAME canonical schemas.py shapes as an agent-fetched or
# Graph-fetched record. Google-family normalization is byte-identical to today
# and stays in _normalize_tool_result.

def _ms_extract_records(data: Any) -> list[Any]:
    """Best-effort extraction of the record list from a Composio Outlook/OneDrive
    response envelope. Handles a bare list, Graph's ``value`` array, and the
    common Composio wrapper keys/nestings defensively."""
    if isinstance(data, list):
        return data
    if not isinstance(data, Mapping):
        return []
    for key in ("value", "messages", "events", "files", "items", "results"):
        val = data.get(key)
        if isinstance(val, list):
            return val
    for key in ("response_data", "data", "result"):
        val = data.get(key)
        if isinstance(val, (Mapping, list)):
            got = _ms_extract_records(val)
            if got:
                return got
    return []


def _ms_normalize_message(m: Mapping[str, Any]) -> dict[str, Any]:
    """Graph message -> canonical schemas.validate_message shape (source outlook)."""
    sender = ""
    frm = m.get("from") or m.get("sender") or {}
    if isinstance(frm, Mapping):
        addr = frm.get("emailAddress", {}) or {}
        if isinstance(addr, Mapping):
            sender = addr.get("address", "") or addr.get("name", "") or ""
    out: dict[str, Any] = {
        "id": m.get("id"),
        "sender": sender or "unknown",
        "subject": m.get("subject") or "(no subject)",
        "date": (m.get("receivedDateTime") or m.get("sentDateTime")
                 or m.get("createdDateTime") or ""),
        "source": "outlook",
    }
    if m.get("conversationId"):
        out["thread_id"] = m.get("conversationId")
    if m.get("bodyPreview") is not None:
        out["snippet"] = m.get("bodyPreview")
    if m.get("categories") is not None:
        out["tags"] = list(m.get("categories") or [])
    if m.get("hasAttachments") is not None:
        out["has_attachments"] = bool(m.get("hasAttachments"))
    if m.get("webLink"):
        out["link"] = m.get("webLink")
    return out


def _ms_normalize_event(e: Mapping[str, Any]) -> dict[str, Any]:
    """Graph event -> canonical schemas.validate_event shape (source outlook)."""
    start = (e.get("start", {}) or {}).get("dateTime") if isinstance(e.get("start"), Mapping) else e.get("start")
    end = (e.get("end", {}) or {}).get("dateTime") if isinstance(e.get("end"), Mapping) else e.get("end")
    attendees: list[str] = []
    for a in e.get("attendees", []) or []:
        addr = ((a.get("emailAddress", {}) or {}).get("address")) if isinstance(a, Mapping) else None
        if addr:
            attendees.append(addr)
    organizer = ((e.get("organizer", {}) or {}).get("emailAddress", {}) or {}).get("address") \
        if isinstance(e.get("organizer"), Mapping) else None
    location = (e.get("location", {}) or {}).get("displayName") if isinstance(e.get("location"), Mapping) else None
    conference_link = ((e.get("onlineMeeting", {}) or {}).get("joinUrl")
                       if isinstance(e.get("onlineMeeting"), Mapping) else None) or location
    out: dict[str, Any] = {
        "id": e.get("id"),
        "title": e.get("subject", "") or "",
        "start": start,
        "end": end,
        "source": "outlook",
    }
    if attendees:
        out["attendees"] = attendees
    if organizer:
        out["organizer"] = organizer
    if location:
        out["location"] = location
    if conference_link:
        out["conference_link"] = conference_link
    if e.get("isCancelled") is not None:
        out["status"] = "cancelled" if e.get("isCancelled") else (e.get("showAs") or "confirmed")
    elif e.get("showAs"):
        out["status"] = e.get("showAs")
    return out


def _ms_normalize_file(f: Mapping[str, Any]) -> dict[str, Any]:
    """Graph DriveItem -> canonical schemas.validate_file shape (source onedrive)."""
    out: dict[str, Any] = {
        "id": f.get("id"),
        "name": f.get("name"),
        "source": "onedrive",
    }
    file_facet = f.get("file") or {}
    if isinstance(file_facet, Mapping) and file_facet.get("mimeType"):
        out["mime_type"] = file_facet.get("mimeType")
    if f.get("lastModifiedDateTime"):
        out["modified"] = f.get("lastModifiedDateTime")
    if f.get("webUrl"):
        out["link"] = f.get("webUrl")
    parent = f.get("parentReference") or {}
    if isinstance(parent, Mapping) and parent.get("id"):
        out["parents"] = [parent.get("id")]
    return out


_MS_NORMALIZERS = {
    "mail_search": _ms_normalize_message,
    "calendar_list": _ms_normalize_event,
    "files_search": _ms_normalize_file,
}


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
        self._validate_config()

        integrations = config.get("integrations", {}) if isinstance(config, Mapping) else {}
        workspace = integrations.get("workspace", {}) if isinstance(integrations, Mapping) else {}
        self.user_id = str(workspace.get("user_id", ""))
        self.toolkits = workspace.get("toolkits", ["gmail", "googlecalendar", "googledrive"])
        if not isinstance(self.toolkits, list):
            self.toolkits = ["gmail", "googlecalendar", "googledrive"]

        # Toolkit family: explicit config wins; else infer from toolkit names.
        self.family = self._resolve_family(workspace)
        # Google family keeps the historical provider name ("composio:mcp") so all
        # existing capability lookups, queues and tests stay valid; microsoft gets
        # a distinct provider name with its own capability entry.
        self._provider_name = (
            "composio_microsoft:mcp" if self.family == "microsoft" else "composio:mcp"
        )
        # Per-operation slug overrides (drift mitigation). Flat {operation: slug}.
        self._tool_slugs: dict[str, str] = {}
        raw_slugs = workspace.get("tool_slugs")
        if isinstance(raw_slugs, Mapping):
            self._tool_slugs = {
                str(k): str(v) for k, v in raw_slugs.items() if v
            }

        mcp_cfg = workspace.get("mcp", {}) if isinstance(workspace, Mapping) else {}
        self.endpoint = str(mcp_cfg.get("endpoint", "https://connect.composio.dev/mcp"))
        self.key_env = str(mcp_cfg.get("key_env", "COMPOSIO_MCP_KEY"))

        self._mcp_client = None
        self._session_meta = load_session_meta(config)

    def _resolve_family(self, workspace: Mapping[str, Any]) -> str:
        """Resolve the toolkit family. Explicit ``family`` wins; otherwise infer
        ``microsoft`` from outlook/one_drive toolkit names and warn once."""
        explicit = workspace.get("family") if isinstance(workspace, Mapping) else None
        if explicit:
            fam = str(explicit).strip().lower()
            if fam in VALID_FAMILIES:
                return fam
            warnings.warn(
                f"integrations.workspace.family={explicit!r} is not one of "
                f"{VALID_FAMILIES}; defaulting to 'google'"
            )
            return "google"
        if any(str(t).strip().lower() in _MICROSOFT_TOOLKITS for t in self.toolkits):
            warnings.warn(
                "integrations.workspace.family not set but toolkits contain "
                "outlook/one_drive — inferring family='microsoft'. Set family "
                "explicitly in company.yaml to silence this warning."
            )
            return "microsoft"
        return "google"

    def _slug_for(self, operation: str) -> str:
        """Return the Composio tool slug for a neutral operation, honouring a
        config ``tool_slugs`` override before the family default."""
        override = self._tool_slugs.get(operation)
        if override:
            return override
        return FAMILY_SLUGS.get(self.family, FAMILY_SLUGS["google"])[operation]

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

    def _execute_composio_tool(self, tool_slug: str, input_data: dict[str, Any],
                               operation: str = "") -> dict[str, Any]:
        """Core helper: call COMPOSIO_MULTI_EXECUTE_TOOL with a tool slug.

        Live-validated payload shape (v0.1.11):
            {"tools": [{"tool_slug": "...", "arguments": {...}}]}

        Note: 'arguments' is the correct field name for COMPOSIO_MULTI_EXECUTE_TOOL.
        Earlier versions used 'input' which worked for some tools (Gmail) but
        failed for others (Calendar Create) because the input dict was not
        passed through to the underlying tool.

        ``operation`` is the neutral op name (e.g. "mail_search"); it is used ONLY
        to build a self-diagnosing message when Composio reports the slug as an
        unknown tool — the raise names the slug and the tool_slugs override path.
        """
        mcp = self._get_mcp()
        result = mcp.call_tool(
            "COMPOSIO_MULTI_EXECUTE_TOOL",
            {
                "tools": [
                    {
                        "tool_slug": tool_slug,
                        "arguments": input_data,
                    }
                ]
            },
        )
        # Extract the actual tool response from results array
        results = result.get("data", {}).get("results", [])
        if results:
            first = results[0] if isinstance(results[0], Mapping) else {}
            resp = first.get("response", {})
            if isinstance(resp, Mapping) and resp.get("successful"):
                return resp.get("data", {})
            # The error can live in results[0]["response"]["error"] (a per-tool
            # failure) OR directly at results[0]["error"] with NO "response"
            # wrapper (a batch-level failure envelope — e.g. a tool whose toolkit
            # has no active connection). Read whichever is present, then fall back
            # to the batch-level message, so a real failure is never masked as an
            # empty successful read.
            err = (
                (resp.get("error") if isinstance(resp, Mapping) else None)
                or first.get("error")
                or result.get("error")
                or "tool execution failed"
            )
            # A wrong/renamed slug (a real risk for the Microsoft slugs) is
            # surfaced with the slug + override path; a missing connection is a
            # hard blocker surfaced with the connect guidance — neither is
            # swallowed into an empty read.
            if _is_unknown_tool_error(err):
                raise ComposioToolError(_unknown_tool_message(operation, tool_slug, err))
            if _is_connection_error(err):
                raise ComposioConnectionError(
                    _connection_error_message(operation, tool_slug, err)
                )
            return {"error": err, "successful": False}
        return result

    def _normalize_records(self, operation: str, slug: str, data: Any) -> Any:
        """Family-aware read normalization. Google reads are byte-identical to
        today (delegates to the static ``_normalize_tool_result``); microsoft
        reads extract the Graph-shaped list and normalize each record to the
        canonical schemas.py shape."""
        if self.family == "microsoft":
            normalizer = _MS_NORMALIZERS.get(operation)
            records = _ms_extract_records(data)
            if normalizer is None:
                return records
            return [normalizer(r) for r in records if isinstance(r, Mapping)]
        return self._normalize_tool_result(slug, data)

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

    # --- Mail ---

    def mail_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        slug = self._slug_for("mail_search")
        try:
            if self.family == "microsoft":
                args = self._ms_mail_search_args(query, max_results)
            else:
                args = {"query": query, "max_results": max_results}
            data = self._execute_composio_tool(slug, args, operation="mail_search")
            return self._normalize_records("mail_search", slug, data)
        except Exception as exc:
            warnings.warn(f"Composio MCP mail_search failed: {exc}")
            return []

    def _ms_mail_search_args(self, query: Any, max_results: int) -> dict[str, Any]:
        """Map an incoming query through the m365 query compiler and lay the
        {folder, filter, search} result into the Outlook slug's arguments.

        The query compiler already supports a per-dialect raw override
        (dict model with ``raw={"m365": {...}}``), which passes through here as an
        exact folder/filter/search — the documented raw-passthrough escape hatch.
        Argument NAMES here are live-verified against OUTLOOK_QUERY_EMAILS, which
        accepts ``folder`` (default inbox), OData ``filter`` and ``top``; a
        ``search`` key is carried through for slug overrides that support Graph
        ``$search`` (QUERY_EMAILS ignores it harmlessly).
        """
        args: dict[str, Any] = {"top": max_results}
        try:
            compiled = compile_query(query, "m365")
        except Exception:
            # Untranslatable free-text (compiler raises) -> fall back to a plain
            # recent-messages listing rather than an opaque failure.
            return args
        if isinstance(compiled, Mapping):
            if compiled.get("filter"):
                args["filter"] = compiled["filter"]
            if compiled.get("search"):
                args["search"] = compiled["search"]
            if compiled.get("folder"):
                args["folder"] = compiled["folder"]
        return args

    @guarded("gmail.draft", target_arg="to", audit_provider="composio",
             audit_tool="GMAIL_CREATE_EMAIL_DRAFT", audit_operation="gmail.create_draft",
             tool_slug="GMAIL_CREATE_EMAIL_DRAFT")
    def mail_create_draft(self, to: str, subject: str, body: str,
                          cc: str | None = None) -> dict[str, Any]:
        slug = self._slug_for("mail_create_draft")
        if self.family == "microsoft":
            args: dict[str, Any] = {
                "subject": subject,
                "body": body,
                "to_recipients": [to] if to else [],
            }
            if cc:
                args["cc_recipients"] = [cc]
        else:
            args = {"to": to, "subject": subject, "body": body}
            if cc:
                args["cc"] = cc
        data = self._execute_composio_tool(slug, args, operation="mail_create_draft")
        return data if isinstance(data, dict) else {}

    # --- Calendar ---

    def calendar_list(self, start: str, end: str) -> list[dict[str, Any]]:
        slug = self._slug_for("calendar_list")
        try:
            if self.family == "microsoft":
                # OUTLOOK_GET_CALENDAR_VIEW requires snake_case start_datetime/
                # end_datetime (ISO 8601) and paginates via ``top`` — live-verified.
                args = {
                    "start_datetime": f"{start}T00:00:00Z" if "T" not in start else start,
                    "end_datetime": f"{end}T23:59:59Z" if "T" not in end else end,
                    "top": 50,
                }
            else:
                args = {
                    "time_min": f"{start}T00:00:00Z" if "T" not in start else start,
                    "time_max": f"{end}T23:59:59Z" if "T" not in end else end,
                    "max_results": 50,
                }
            data = self._execute_composio_tool(slug, args, operation="calendar_list")
            return self._normalize_records("calendar_list", slug, data)
        except Exception as exc:
            warnings.warn(f"Composio MCP calendar_list failed: {exc}")
            return []

    @guarded("calendar.create", target_arg="title", audit_provider="composio",
             audit_tool="GOOGLECALENDAR_CREATE_EVENT", tool_slug="GOOGLECALENDAR_CREATE_EVENT")
    def calendar_create(self, title: str, start: str, end: str,
                        attendees: list[str] | None = None,
                        description: str | None = None) -> dict[str, Any]:
        slug = self._slug_for("calendar_create")
        if self.family == "microsoft":
            args: dict[str, Any] = {
                "subject": title,
                "start_datetime": f"{start}T00:00:00Z" if "T" not in start else start,
                "end_datetime": f"{end}T23:59:59Z" if "T" not in end else end,
            }
            if attendees:
                args["attendees"] = attendees
            if description:
                args["body"] = description
        else:
            args = {
                "summary": title,
                "start_datetime": f"{start}T00:00:00Z" if "T" not in start else start,
                "end_datetime": f"{end}T23:59:59Z" if "T" not in end else end,
            }
            if attendees:
                args["attendees"] = attendees
            if description:
                args["description"] = description
        data = self._execute_composio_tool(slug, args, operation="calendar_create")
        return data if isinstance(data, dict) else {}

    @guarded("calendar.update", target_arg="event_id", audit_provider="composio",
             audit_tool="GOOGLECALENDAR_UPDATE_EVENT", tool_slug="GOOGLECALENDAR_UPDATE_EVENT")
    def calendar_update(self, event_id: str, **fields: Any) -> dict[str, Any]:
        slug = self._slug_for("calendar_update")
        args = {"event_id": event_id, **fields}
        data = self._execute_composio_tool(slug, args, operation="calendar_update")
        return data if isinstance(data, dict) else {}

    # --- Files ---

    def files_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        slug = self._slug_for("files_search")
        try:
            if self.family == "microsoft":
                # ONE_DRIVE_SEARCH_ITEMS requires ``q`` (the search text) and
                # paginates via ``top`` — NOT query/max_results.
                args = {"q": query, "top": max_results}
            else:
                args = {"query": query, "max_results": max_results}
            data = self._execute_composio_tool(slug, args, operation="files_search")
            return self._normalize_records("files_search", slug, data)
        except Exception as exc:
            warnings.warn(f"Composio MCP files_search failed: {exc}")
            return []

    @guarded("drive.upload", target_arg="file_path", audit_provider="composio",
             audit_tool="GOOGLEDRIVE_UPLOAD_FILE", tool_slug="GOOGLEDRIVE_UPLOAD_FILE")
    def files_upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        slug = self._slug_for("files_upload")
        args: dict[str, Any] = {"file_path": file_path}
        if parent_id:
            args["parent_id"] = parent_id
        data = self._execute_composio_tool(slug, args, operation="files_upload")
        return data if isinstance(data, dict) else {}

    @guarded("drive.download", target_arg="file_id", audit_provider="composio",
             audit_tool="GOOGLEDRIVE_DOWNLOAD_FILE", tool_slug="GOOGLEDRIVE_DOWNLOAD_FILE")
    def files_download(self, file_id: str, output_path: str) -> dict[str, Any]:
        slug = self._slug_for("files_download")
        data = self._execute_composio_tool(slug, {
            "file_id": file_id,
            "output_path": output_path,
        }, operation="files_download")
        return {"path": output_path, **(data if isinstance(data, dict) else {})}

    # --- Health ---

    def health_check(self) -> bool:
        try:
            mcp = self._get_mcp()
            mcp.initialize()
            return True
        except Exception:
            return False


def get_composio_client(config: Any) -> "ComposioMCPWorkspaceClient":
    """Factory: return the Composio MCP client.

    This is the single entry point for Composio workspace operations.
    The legacy composio_workspace.py shim delegates here.
    """
    return ComposioMCPWorkspaceClient(config)