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
from composio_family import (  # noqa: E402
    VALID_FAMILIES,
    MICROSOFT_TOOLKITS as _MICROSOFT_TOOLKITS,
    _resolve_composio_family,
    warn_family_toolkit_mismatch,
)

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
        # Catalog-verified against docs.composio.dev/toolkits/gmail (v0.3.13).
        "mail_send": "GMAIL_SEND_EMAIL",
        "mail_list_tags": "GMAIL_LIST_LABELS",
        "mail_create_tag": "GMAIL_CREATE_LABEL",
        "mail_modify_labels": "GMAIL_ADD_LABEL_TO_EMAIL",  # archive/unarchive/tag
        "mail_trash": "GMAIL_MOVE_TO_TRASH",
        "mail_untrash": "GMAIL_UNTRASH_MESSAGE",
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
    # COMPOSIO_SEARCH_TOOLS / COMPOSIO_GET_TOOL_SCHEMAS. Cleanup primitives
    # (mail_move → archive/trash, files_trash) were catalog-verified against
    # docs.composio.dev Outlook/OneDrive toolkits (v0.3.9). The earlier defaults
    # used a doubled toolkit prefix (OUTLOOK_OUTLOOK_*, ONE_DRIVE_ONE_DRIVE_*)
    # that no real slug carries. Any slug is still overridable per-operation via
    # integrations.workspace.tool_slugs without a code change.
    "microsoft": {
        "mail_search": "OUTLOOK_QUERY_EMAILS",
        "mail_create_draft": "OUTLOOK_CREATE_DRAFT",
        "mail_send": "OUTLOOK_SEND_EMAIL",
        "mail_list_folders": "OUTLOOK_LIST_MAIL_FOLDERS",
        "mail_move": "OUTLOOK_MOVE_MESSAGE",
        "mail_list_tags": "OUTLOOK_GET_MASTER_CATEGORIES",
        "mail_create_tag": "OUTLOOK_CREATE_USER_MASTER_CATEGORY",
        "mail_get_message": "OUTLOOK_GET_MESSAGE",
        "mail_update": "OUTLOOK_UPDATE_EMAIL",
        "calendar_list": "OUTLOOK_GET_CALENDAR_VIEW",
        "calendar_create": "OUTLOOK_CALENDAR_CREATE_EVENT",
        "calendar_update": "OUTLOOK_UPDATE_CALENDAR_EVENT",
        "calendar_delete": "OUTLOOK_DELETE_CALENDAR_EVENT",
        "files_search": "ONE_DRIVE_SEARCH_ITEMS",
        # MCP-native text create — no Files API / FileUploadable staging needed.
        # See https://composio.dev/toolkits/one_drive (CREATE_TEXT_FILE).
        "files_upload": "ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE",
        # Binary / large files: FileUploadable {name,mimetype,s3key} or source_url.
        "files_upload_binary": "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE",
        "files_download": "ONE_DRIVE_DOWNLOAD_FILE",
        "files_trash": "ONE_DRIVE_DELETE_ITEM",
    },
}

# Suffixes that can go through ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE (plain-text
# content over MCP). Binary uploads still need FileUploadable staging or a
# public source_url on ONE_DRIVE_ONEDRIVE_UPLOAD_FILE.
_MS_TEXT_UPLOAD_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".log", ".html", ".htm", ".xml", ".css", ".js", ".ts", ".py", ".sh",
    ".env", ".ini", ".cfg", ".toml",
})

# Graph / Composio well-known mail folder names (valid as destination_id).
_MS_WELL_KNOWN_FOLDERS = frozenset({
    "inbox", "drafts", "sentitems", "deleteditems", "junkemail", "archive",
    "outbox", "clutter", "conflicts", "conversationhistory", "localfailures",
    "msgfolderroot", "recoverableitemsdeletions", "scheduled", "searchfolders",
    "serverfailures", "syncissues",
})


class ComposioReadError(RuntimeError):
    """Hard failure on a Composio workspace read (mail/calendar/files).

    Carries the neutral ``operation`` name and the ``original`` error so callers
    (daily briefing, weekly review) can mark a section unavailable instead of
    treating an empty list as "no data".
    """

    def __init__(
        self,
        operation: str,
        original: BaseException,
        message: str | None = None,
    ) -> None:
        self.operation = operation
        self.original = original
        super().__init__(message or f"Composio {operation} failed: {original}")


class ComposioToolError(ComposioReadError):
    """Raised when Composio reports an unknown/invalid tool slug for an operation.

    The message names the failing slug AND the tool_slugs config override path so
    a catalog drift is self-diagnosing (reads warn with it; guarded writes surface
    it as the ActionResult error).
    """

    def __init__(self, message: str, *, operation: str = "", original: BaseException | None = None) -> None:
        # Tool errors are raised with a fully-formed message; stash a sentinel
        # original so ComposioReadError's contract (operation + original) holds.
        err = original if original is not None else RuntimeError(message)
        super().__init__(operation or "<operation>", err, message=message)


class ComposioConnectionError(ComposioReadError):
    """Raised when Composio reports the toolkit has no active connection.

    A missing connection is a HARD, persistent operator problem (unlike a
    transient rate-limit), so it must NOT be swallowed into an empty successful
    read — that would falsely certify a capability the mailbox/drive cannot serve.
    Reads propagate it (so verification honestly fails the check); guarded writes
    surface it as the ActionResult error.
    """

    def __init__(self, message: str, *, operation: str = "", original: BaseException | None = None) -> None:
        err = original if original is not None else RuntimeError(message)
        super().__init__(operation or "<operation>", err, message=message)


# Composio Outlook slugs known to ignore Graph ``$search`` / KQL. When the active
# mail_search slug is in this set, text-search components are dropped (with a
# warning) and any recoverable OData filter is preferred. Override
# integrations.workspace.tool_slugs.mail_search to a KQL-capable slug when
# Composio adds one, or set mail_search_supports_text_search: true.
_MS_MAIL_SLUGS_WITHOUT_TEXT_SEARCH = frozenset({"OUTLOOK_QUERY_EMAILS"})


def _is_connection_error(err: Any) -> bool:
    """True if a Composio error payload/string looks like a no-active-connection
    error (the toolkit was never connected / the OAuth link is still pending).

    Checked BEFORE unknown-tool classification so phrases like
    ``connection not found`` are not swallowed by a bare ``not found`` needle.
    """
    text = str(err).lower()
    needles = (
        "no active connection", "no connection found", "connection not found",
        "not connected", "establish a connection", "no connected account",
    )
    return any(n in text for n in needles)


def _is_unknown_tool_error(err: Any) -> bool:
    """True if a Composio error payload/string looks like an unknown-tool error.

    Needles are tool-oriented only — bare ``not found`` / ``does not exist``
    would collide with connection and resource-missing messages.
    """
    text = str(err).lower()
    needles = (
        "unknown tool", "no such tool", "invalid tool", "tool not found",
        "no tool", "unrecognized tool", "not a valid tool",
        "invalid tool_slug", "unknown slug",
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
    for key in ("value", "messages", "events", "files", "items", "results", "labels"):
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


def _ms_normalize_folder(f: Mapping[str, Any]) -> dict[str, Any]:
    """Graph mailFolder → compact folder dict for organise workflows."""
    return {
        "id": f.get("id"),
        "name": f.get("displayName") or f.get("name") or "",
        "parent_id": f.get("parentFolderId"),
        "unread": f.get("unreadItemCount"),
        "total": f.get("totalItemCount"),
        "child_count": f.get("childFolderCount"),
        "hidden": bool(f.get("isHidden")) if f.get("isHidden") is not None else None,
    }


_MS_NORMALIZERS = {
    "mail_search": _ms_normalize_message,
    "calendar_list": _ms_normalize_event,
    "files_search": _ms_normalize_file,
    "mail_list_folders": _ms_normalize_folder,
}


def _ms_recipient(address: str) -> dict[str, dict[str, str]]:
    return {"emailAddress": {"address": address}}


def _ms_datetime(value: str, *, is_end: bool = False) -> dict[str, str]:
    """Convert a date or datetime string to a Graph dateTime object.

    Date-only strings get ``T00:00:00Z`` for starts and ``T23:59:59Z`` for ends
    so all-day events have a non-zero duration.
    """
    if "T" not in value:
        suffix = "T23:59:59Z" if is_end else "T00:00:00Z"
    else:
        suffix = ""
    return {
        "dateTime": f"{value}{suffix}" if suffix else value,
        "timeZone": "UTC",
    }


def _ms_attendee(address: str) -> dict[str, Any]:
    return {"emailAddress": {"address": address}, "type": "required"}


def _ms_entity_id(data: Any, fallback: str = "") -> str:
    """Extract an entity id from a Composio Microsoft write/move payload.

    Prefers top-level ``id``, then common nested envelopes. Used for move
    ``restore_target`` and for draft/event/file ids that ``workspace_verify``
    reads from ActionResult.data.
    """
    if isinstance(data, Mapping):
        top = data.get("id")
        if isinstance(top, str) and top:
            return top
        for key in ("data", "response_data", "result", "message", "event", "item"):
            nested = data.get(key)
            if isinstance(nested, Mapping):
                nid = nested.get("id")
                if isinstance(nid, str) and nid:
                    return nid
        value = data.get("value")
        if isinstance(value, Mapping):
            nid = value.get("id")
            if isinstance(nid, str) and nid:
                return nid
    return fallback


def _ms_moved_message_id(data: Any, fallback: str) -> str:
    """Extract the post-move message id from an OUTLOOK_MOVE_MESSAGE payload."""
    return _ms_entity_id(data, fallback)


def _ms_with_id(data: Any) -> dict[str, Any]:
    """Ensure a write payload dict exposes a top-level ``id`` when present."""
    out = dict(data) if isinstance(data, Mapping) else {}
    eid = _ms_entity_id(out, "")
    if eid:
        out["id"] = eid
    return out


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
        warn_family_toolkit_mismatch(self.family, self.toolkits)
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
        """Resolve the toolkit family via the shared helper (warns on invalid /
        inferred family)."""
        return _resolve_composio_family(workspace, toolkits=self.toolkits)

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
            # hard blocker surfaced with the connect guidance. Soft failures
            # (rate limits, auth, malformed payloads) also raise — never
            # normalize to an empty successful read (that falsely certifies
            # read_ready). Connection is classified before unknown-tool so
            # "connection not found" is not mis-tagged as a slug error.
            if _is_connection_error(err):
                raise ComposioConnectionError(
                    _connection_error_message(operation, tool_slug, err),
                    operation=operation or "<operation>",
                    original=RuntimeError(str(err)),
                )
            if _is_unknown_tool_error(err):
                raise ComposioToolError(
                    _unknown_tool_message(operation, tool_slug, err),
                    operation=operation or "<operation>",
                    original=RuntimeError(str(err)),
                )
            raise RuntimeError(
                f"Composio tool execution failed for slug {tool_slug!r} "
                f"(operation {(operation or '<operation>')!r}): {err}"
            )
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
        self.last_mail_search_meta: dict[str, Any] = {"degraded": False}
        try:
            if self.family == "microsoft":
                args, meta = self._ms_mail_search_args(query, max_results)
                self.last_mail_search_meta = meta
            else:
                args = {"query": query, "max_results": max_results}
            data = self._execute_composio_tool(slug, args, operation="mail_search")
            return self._normalize_records("mail_search", slug, data)
        except (ComposioConnectionError, ComposioToolError):
            raise
        except Exception as exc:
            # Rate limits, auth errors, malformed payloads, etc. must NOT look
            # like an empty successful read — wrap and propagate so callers
            # (daily briefing) can mark the section unavailable.
            raise ComposioReadError("mail_search", exc) from exc

    def _mail_search_supports_text_search(self) -> bool:
        """Whether the active Outlook mail_search slug accepts a KQL/search arg.

        Default OUTLOOK_QUERY_EMAILS ignores ``search``. Operators can:
          * override ``integrations.workspace.tool_slugs.mail_search`` to a
            KQL-capable Composio slug (any slug outside the known-ignore set), or
          * set ``integrations.workspace.mail_search_supports_text_search: true``
            when the active slug gains search support without a rename.
        """
        integrations = self.config.get("integrations", {}) if isinstance(self.config, Mapping) else {}
        workspace = integrations.get("workspace", {}) if isinstance(integrations, Mapping) else {}
        if isinstance(workspace, Mapping) and "mail_search_supports_text_search" in workspace:
            return bool(workspace.get("mail_search_supports_text_search"))
        return self._slug_for("mail_search") not in _MS_MAIL_SLUGS_WITHOUT_TEXT_SEARCH

    def _ms_mail_search_args(
        self, query: Any, max_results: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Map an incoming query through the m365 query compiler and lay the
        {folder, filter, search} result into the Outlook slug's arguments.

        Returns ``(args, meta)`` where ``meta`` carries query-compilation honesty
        flags for callers (daily briefing). When text search is dropped under
        OUTLOOK_QUERY_EMAILS, meta is
        ``{"degraded": True, "dropped_constraints": ["text_search"]}``.

        The query compiler already supports a per-dialect raw override
        (dict model with ``raw={"m365": {...}}``), which passes through here as an
        exact folder/filter/search — the documented raw-passthrough escape hatch.
        Argument NAMES here are live-verified against OUTLOOK_QUERY_EMAILS, which
        accepts ``folder`` (default inbox), OData ``filter`` and ``top``. That
        slug ignores ``search``; when text search is required we prefer any
        recoverable OData filter and warn, unless the operator has pointed
        ``tool_slugs.mail_search`` at a KQL-capable slug (or set
        ``mail_search_supports_text_search: true``).
        """
        args: dict[str, Any] = {"top": max_results}
        ok_meta: dict[str, Any] = {"degraded": False}
        slug = self._slug_for("mail_search")
        supports_search = self._mail_search_supports_text_search()
        try:
            # When the slug cannot search, disable the filter+search KQL fold so
            # filter-eligible clauses remain visible and can be preferred.
            compiled = compile_query(
                query, "m365", fold_filter_search=supports_search,
            )
        except Exception as exc:
            # Untranslatable free-text (compiler raises) -> fall back to a plain
            # recent-messages listing, but warn so the operator knows the query
            # was broadened rather than failing silently.
            warnings.warn(
                f"m365 mail query compile failed ({exc!r}); broadening to "
                f"list recent mail (top={max_results})",
                UserWarning,
                stacklevel=2,
            )
            return args, {
                "degraded": True,
                "dropped_constraints": ["uncompiled_query"],
            }
        if not isinstance(compiled, Mapping):
            return args, ok_meta

        has_filter = bool(compiled.get("filter"))
        has_search = bool(compiled.get("search"))

        if has_search and not supports_search:
            warnings.warn(
                f"Composio mail_search slug {slug!r} does not support text "
                f"search; query will be broadened. Override via "
                f"integrations.workspace.tool_slugs.mail_search (KQL-capable "
                f"slug) or set mail_search_supports_text_search: true when "
                f"available.",
                UserWarning,
                stacklevel=2,
            )
            if has_filter:
                warnings.warn(
                    "text search dropped; keeping OData filter (more specific)",
                    UserWarning,
                    stacklevel=2,
                )
            # Prefer filter when both are present; otherwise drop search and keep
            # folder/top only (broadened listing).
            if compiled.get("filter"):
                args["filter"] = compiled["filter"]
            if compiled.get("folder"):
                args["folder"] = compiled["folder"]
            return args, {
                "degraded": True,
                "dropped_constraints": ["text_search"],
            }

        if compiled.get("filter"):
            args["filter"] = compiled["filter"]
        if compiled.get("search"):
            args["search"] = compiled["search"]
        if compiled.get("folder"):
            args["folder"] = compiled["folder"]
        return args, ok_meta

    @guarded("gmail.draft", target_arg="to", audit_provider="composio",
             audit_tool=lambda self: self._slug_for("mail_create_draft"),
             audit_operation="gmail.create_draft",
             tool_slug=lambda self: self._slug_for("mail_create_draft"))
    def mail_create_draft(self, to: str, subject: str, body: str,
                          cc: str | None = None) -> dict[str, Any]:
        slug = self._slug_for("mail_create_draft")
        if self.family == "microsoft":
            # OUTLOOK_CREATE_DRAFT catalog shape (not raw Graph message JSON):
            # body is a string + is_html; recipients are string arrays.
            args: dict[str, Any] = {
                "subject": subject,
                "body": body,
                "is_html": True,
                "to_recipients": [to] if to else [],
            }
            if cc:
                args["cc_recipients"] = [cc]
        else:
            args = {"to": to, "subject": subject, "body": body}
            if cc:
                args["cc"] = cc
        data = self._execute_composio_tool(slug, args, operation="mail_create_draft")
        return _ms_with_id(data) if self.family == "microsoft" else (
            data if isinstance(data, dict) else {}
        )

    @guarded("mail.send", target_arg="to", audit_provider="composio",
             audit_tool=lambda self: self._cleanup_slug("mail_send"),
             tool_slug=lambda self: self._cleanup_slug("mail_send"),
             block_error=(
                 "cancelled by guardrail (requires explicit user approval; "
                 "use send_email.py prepare→approve→execute or set "
                 "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1)"
             ))
    def mail_send(self, to: str, subject: str, body: str,
                  cc: str | None = None) -> dict[str, Any]:
        """Send email (destructive / approval-gated).

        Microsoft → ``OUTLOOK_SEND_EMAIL``; Google → ``GMAIL_SEND_EMAIL``.
        """
        slug = self._slug_for("mail_send")
        if self.family == "microsoft":
            args: dict[str, Any] = {
                "to": to,
                "subject": subject,
                "body": body,
                "is_html": False,
                "save_to_sent_items": True,
            }
            if cc:
                args["cc_emails"] = [
                    part.strip() for part in cc.split(",") if part.strip()
                ]
        else:
            args = {
                "recipient_email": to,
                "subject": subject,
                "body": body,
                "is_html": False,
            }
            if cc:
                args["cc"] = [
                    part.strip() for part in cc.split(",") if part.strip()
                ]
        self._execute_composio_tool(slug, args, operation="mail_send")
        return {"status": "sent", "to": to}

    def _require_microsoft_cleanup(self, operation: str) -> None:
        """Ops that remain Microsoft-only (Outlook folders / calendar delete / …).

        Google Gmail archive/trash/tags/send are wired separately (v0.3.13).
        """
        if self.family != "microsoft":
            raise NotImplementedError(
                f"{operation} is only implemented for Composio Microsoft "
                f"(Outlook/OneDrive); current family is {self.family!r}"
            )

    def _cleanup_slug(self, operation: str) -> str:
        """Resolve a cleanup/organise slug for the active family."""
        try:
            return self._slug_for(operation)
        except KeyError:
            return operation

    def _google_modify_labels(
        self,
        message_id: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        """GMAIL_ADD_LABEL_TO_EMAIL — archive/unarchive/tag via label ids."""
        slug = self._slug_for("mail_modify_labels")
        args: dict[str, Any] = {"message_id": message_id}
        if add:
            args["add_label_ids"] = add
        if remove:
            args["remove_label_ids"] = remove
        self._execute_composio_tool(slug, args, operation="mail_modify_labels")
        return {"id": message_id, "add_label_ids": add or [], "remove_label_ids": remove or []}

    def mail_list_folders(self, include_hidden: bool = False,
                          max_results: int = 100) -> list[dict[str, Any]]:
        """List top-level Outlook mail folders (OUTLOOK_LIST_MAIL_FOLDERS)."""
        self._require_microsoft_cleanup("mail_list_folders")
        slug = self._slug_for("mail_list_folders")
        try:
            args: dict[str, Any] = {"top": max_results}
            if include_hidden:
                args["include_hidden_folders"] = True
            data = self._execute_composio_tool(
                slug, args, operation="mail_list_folders",
            )
            return self._normalize_records("mail_list_folders", slug, data)
        except (ComposioConnectionError, ComposioToolError):
            raise
        except Exception as exc:
            raise ComposioReadError("mail_list_folders", exc) from exc

    def mail_resolve_folder(self, name_or_id: str) -> dict[str, Any] | None:
        """Resolve a folder display name or id to ``{id, name, ...}``.

        Resolution order: well-known names (``inbox``, ``archive``, …) →
        case-insensitive display-name match against ``mail_list_folders`` →
        otherwise assume the token is already an opaque folder id. The
        display-name lookup runs BEFORE the id fallback so a long, space-free
        folder name (e.g. ``Newsletters_and_Promotions``) is not mistaken for
        an opaque id and silently turned into an invalid destination.
        """
        token = (name_or_id or "").strip()
        if not token:
            return None
        lower = token.lower()
        if lower in _MS_WELL_KNOWN_FOLDERS:
            return {"id": lower, "name": lower, "well_known": True}
        matches = [
            f for f in self.mail_list_folders(max_results=200)
            if str(f.get("name", "")).lower() == lower
        ]
        if matches:
            return matches[0]
        # Not a visible display name — assume it is already a folder id. Opaque
        # Graph/Composio ids are long, space-free strings; a short token with
        # spaces is neither a name we can see nor a plausible id.
        if len(token) >= 20 and " " not in token:
            return {"id": token, "name": token, "well_known": False}
        return None

    def _ms_mail_move(self, message_id: str, destination: str) -> dict[str, Any]:
        """Move an Outlook message via OUTLOOK_MOVE_MESSAGE.

        ``destination`` may be a well-known name (``inbox``, ``archive``,
        ``deleteditems``, …) or a folder id from ``mail_list_folders``. Prefer
        folder ids for custom folders — display names are not valid destinations.
        """
        self._require_microsoft_cleanup("mail_move")
        slug = self._slug_for("mail_move")
        data = self._execute_composio_tool(
            slug,
            {"message_id": message_id, "destination_id": destination},
            operation="mail_move",
        )
        moved_id = _ms_moved_message_id(data, message_id)
        return {
            "id": moved_id,
            "destination": destination,
            "restore_target": moved_id,
            "reversible": True,
        }

    @guarded("mail.move", target_arg="message_id", audit_provider="composio",
             audit_tool=lambda self: self._ms_cleanup_slug("mail_move"),
             tool_slug=lambda self: self._ms_cleanup_slug("mail_move"))
    def mail_move_to_folder(self, message_id: str, folder_id: str) -> dict[str, Any]:
        """Move a message to any folder id or well-known name (Phase 3)."""
        return self._ms_mail_move(message_id, folder_id)

    def mail_list_tags(self) -> list[dict[str, Any]]:
        """List tags: Outlook master categories or Gmail labels."""
        slug = self._slug_for("mail_list_tags")
        try:
            if self.family == "microsoft":
                data = self._execute_composio_tool(
                    slug, {"top": 100}, operation="mail_list_tags",
                )
                records = _ms_extract_records(data)
                out: list[dict[str, Any]] = []
                for c in records:
                    if not isinstance(c, Mapping):
                        continue
                    name = str(c.get("displayName") or c.get("name") or "")
                    if not name:
                        continue
                    # Tag id IS the category displayName (native m365 contract).
                    out.append({
                        "id": name,
                        "name": name,
                        "displayName": name,
                        "type": "user",
                        "color": c.get("color"),
                        "graph_id": c.get("id"),
                    })
                return out

            data = self._execute_composio_tool(
                slug, {}, operation="mail_list_tags",
            )
            records = _ms_extract_records(data)
            if not records and isinstance(data, Mapping):
                # GMAIL_LIST_LABELS often returns {labels: [...]}
                labels = data.get("labels")
                if isinstance(labels, list):
                    records = labels
            out = []
            for c in records:
                if not isinstance(c, Mapping):
                    continue
                lid = str(c.get("id") or "")
                name = str(c.get("name") or "")
                if not lid and not name:
                    continue
                out.append({
                    "id": lid or name,
                    "name": name or lid,
                    "type": str(c.get("type") or "user"),
                })
            return out
        except (ComposioConnectionError, ComposioToolError):
            raise
        except Exception as exc:
            raise ComposioReadError("mail_list_tags", exc) from exc

    @guarded("mail.create_tag", target_arg="name", audit_provider="composio",
             audit_tool=lambda self: self._cleanup_slug("mail_create_tag"),
             tool_slug=lambda self: self._cleanup_slug("mail_create_tag"))
    def mail_create_tag(self, name: str) -> dict[str, Any]:
        """Create Outlook master category or Gmail label."""
        slug = self._slug_for("mail_create_tag")
        if self.family == "microsoft":
            data = self._execute_composio_tool(
                slug,
                {"display_name": name, "color": "preset0"},
                operation="mail_create_tag",
            )
            payload = data if isinstance(data, Mapping) else {}
            return {
                "id": name,
                "name": name,
                "graph_id": payload.get("id") if isinstance(payload, Mapping) else None,
            }

        data = self._execute_composio_tool(
            slug, {"label_name": name}, operation="mail_create_tag",
        )
        payload = data if isinstance(data, Mapping) else {}
        label_id = (
            payload.get("id")
            or payload.get("labelId")
            or payload.get("label_id")
            or name
        )
        return {"id": label_id, "name": name}

    @guarded("mail.tag", target_arg="message_id", audit_provider="composio",
             audit_tool=lambda self: (
                 self._cleanup_slug("mail_update")
                 if self.family == "microsoft"
                 else self._cleanup_slug("mail_modify_labels")
             ),
             tool_slug=lambda self: (
                 self._cleanup_slug("mail_update")
                 if self.family == "microsoft"
                 else self._cleanup_slug("mail_modify_labels")
             ))
    def mail_tag(self, message_id: str, tag_id: str) -> dict[str, Any]:
        """Apply a tag: Outlook category (append) or Gmail label id."""
        if self.family == "microsoft":
            get_slug = self._slug_for("mail_get_message")
            current = self._execute_composio_tool(
                get_slug,
                {"message_id": message_id, "select": ["categories"]},
                operation="mail_get_message",
            )
            existing: list[str] = []
            if isinstance(current, Mapping):
                cats = current.get("categories")
                if cats is None and isinstance(current.get("data"), Mapping):
                    cats = current["data"].get("categories")
                if isinstance(cats, list):
                    existing = [str(c) for c in cats if c]
            if tag_id not in existing:
                existing.append(tag_id)
            update_slug = self._slug_for("mail_update")
            self._execute_composio_tool(
                update_slug,
                {"message_id": message_id, "categories": existing},
                operation="mail_tag",
            )
            return {"id": message_id, "categories": existing}

        # Gmail: tag_id must be a label id (Label_…), not the display name.
        result = self._google_modify_labels(message_id, add=[tag_id])
        return {"id": message_id, "label_id": tag_id, **result}

    # Back-compat alias used by older @guarded call sites / tests.
    def _ms_cleanup_slug(self, operation: str) -> str:
        return self._cleanup_slug(operation)

    @guarded("mail.archive", target_arg="message_id", audit_provider="composio",
             audit_tool=lambda self: (
                 self._cleanup_slug("mail_move")
                 if self.family == "microsoft"
                 else self._cleanup_slug("mail_modify_labels")
             ),
             tool_slug=lambda self: (
                 self._cleanup_slug("mail_move")
                 if self.family == "microsoft"
                 else self._cleanup_slug("mail_modify_labels")
             ))
    def mail_archive(self, message_id: str) -> dict[str, Any]:
        if self.family == "microsoft":
            return self._ms_mail_move(message_id, "archive")
        out = self._google_modify_labels(message_id, remove=["INBOX"])
        return {**out, "destination": "archive", "restore_target": message_id, "reversible": True}

    @guarded("mail.unarchive", target_arg="message_id", audit_provider="composio",
             audit_tool=lambda self: (
                 self._cleanup_slug("mail_move")
                 if self.family == "microsoft"
                 else self._cleanup_slug("mail_modify_labels")
             ),
             tool_slug=lambda self: (
                 self._cleanup_slug("mail_move")
                 if self.family == "microsoft"
                 else self._cleanup_slug("mail_modify_labels")
             ))
    def mail_unarchive(self, message_id: str) -> dict[str, Any]:
        if self.family == "microsoft":
            return self._ms_mail_move(message_id, "inbox")
        out = self._google_modify_labels(message_id, add=["INBOX"])
        return {**out, "destination": "inbox", "restore_target": message_id, "reversible": True}

    @guarded("mail.trash", target_arg="message_id", audit_provider="composio",
             audit_tool=lambda self: (
                 self._cleanup_slug("mail_move")
                 if self.family == "microsoft"
                 else self._cleanup_slug("mail_trash")
             ),
             tool_slug=lambda self: (
                 self._cleanup_slug("mail_move")
                 if self.family == "microsoft"
                 else self._cleanup_slug("mail_trash")
             ))
    def mail_trash(self, message_id: str) -> dict[str, Any]:
        """Soft-delete: Outlook deleteditems, or GMAIL_MOVE_TO_TRASH."""
        if self.family == "microsoft":
            return self._ms_mail_move(message_id, "deleteditems")
        slug = self._slug_for("mail_trash")
        self._execute_composio_tool(
            slug, {"message_id": message_id}, operation="mail_trash",
        )
        return {
            "id": message_id,
            "destination": "trash",
            "restore_target": message_id,
            "reversible": True,
        }

    @guarded("mail.untrash", target_arg="message_id", audit_provider="composio",
             audit_tool=lambda self: (
                 self._cleanup_slug("mail_move")
                 if self.family == "microsoft"
                 else self._cleanup_slug("mail_untrash")
             ),
             tool_slug=lambda self: (
                 self._cleanup_slug("mail_move")
                 if self.family == "microsoft"
                 else self._cleanup_slug("mail_untrash")
             ))
    def mail_untrash(self, message_id: str) -> dict[str, Any]:
        if self.family == "microsoft":
            return self._ms_mail_move(message_id, "inbox")
        slug = self._slug_for("mail_untrash")
        self._execute_composio_tool(
            slug, {"message_id": message_id}, operation="mail_untrash",
        )
        return {
            "id": message_id,
            "destination": "inbox",
            "restore_target": message_id,
            "reversible": True,
        }

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
        except (ComposioConnectionError, ComposioToolError):
            raise
        except Exception as exc:
            raise ComposioReadError("calendar_list", exc) from exc

    @guarded("calendar.create", target_arg="title", audit_provider="composio",
             audit_tool=lambda self: self._slug_for("calendar_create"),
             tool_slug=lambda self: self._slug_for("calendar_create"))
    def calendar_create(self, title: str, start: str, end: str,
                        attendees: list[str] | None = None,
                        description: str | None = None) -> dict[str, Any]:
        slug = self._slug_for("calendar_create")
        if self.family == "microsoft":
            # OUTLOOK_CALENDAR_CREATE_EVENT catalog shape: start_datetime /
            # end_datetime + required time_zone (not Graph start/end objects).
            start_dt = f"{start}T10:00:00" if "T" not in start else start
            end_dt = f"{end}T11:00:00" if "T" not in end else end
            args: dict[str, Any] = {
                "subject": title,
                "start_datetime": start_dt,
                "end_datetime": end_dt,
                "time_zone": "UTC",
            }
            if attendees:
                args["attendees_info"] = list(attendees)
            if description:
                args["body"] = description
                args["is_html"] = True
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
        return _ms_with_id(data) if self.family == "microsoft" else (
            data if isinstance(data, dict) else {}
        )

    @guarded("calendar.update", target_arg="event_id", audit_provider="composio",
             audit_tool=lambda self: self._slug_for("calendar_update"),
             tool_slug=lambda self: self._slug_for("calendar_update"))
    def calendar_update(self, event_id: str, **fields: Any) -> dict[str, Any]:
        slug = self._slug_for("calendar_update")
        if self.family == "microsoft":
            # OUTLOOK_UPDATE_CALENDAR_EVENT: start_datetime/end_datetime strings;
            # body remains a Graph-ish {contentType, content} object per catalog.
            args: dict[str, Any] = {"event_id": event_id}
            for key, value in fields.items():
                if key in ("title", "summary", "subject"):
                    args["subject"] = value
                elif key == "start" and value:
                    raw = str(value)
                    args["start_datetime"] = f"{raw}T10:00:00" if "T" not in raw else raw
                    args.setdefault("time_zone", "UTC")
                elif key == "end" and value:
                    raw = str(value)
                    args["end_datetime"] = f"{raw}T11:00:00" if "T" not in raw else raw
                    args.setdefault("time_zone", "UTC")
                elif key == "description" and value is not None:
                    args["body"] = {"contentType": "HTML", "content": str(value)}
                elif key == "attendees" and isinstance(value, list):
                    args["attendees"] = [
                        {"emailAddress": {"address": str(a)}, "type": "required"}
                        for a in value
                    ]
                else:
                    args[key] = value
        else:
            args = {"event_id": event_id, **fields}
        data = self._execute_composio_tool(slug, args, operation="calendar_update")
        return _ms_with_id(data) if self.family == "microsoft" else (
            data if isinstance(data, dict) else {}
        )

    @guarded("calendar.delete", target_arg="event_id", audit_provider="composio",
             audit_tool=lambda self: self._ms_cleanup_slug("calendar_delete"),
             tool_slug=lambda self: self._ms_cleanup_slug("calendar_delete"),
             block_error="cancelled by guardrail (requires CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1)")
    def calendar_delete(self, event_id: str) -> dict[str, Any]:
        """Delete a calendar event (Microsoft family; used for verify cleanup).

        Distinct from ``calendar.cancel`` (unsupported — no restore path).
        Destructive: requires ``CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1``.
        """
        self._require_microsoft_cleanup("calendar_delete")
        slug = self._slug_for("calendar_delete")
        self._execute_composio_tool(
            slug, {"event_id": event_id}, operation="calendar_delete",
        )
        return {"id": event_id, "deleted": True}

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
        except (ComposioConnectionError, ComposioToolError):
            raise
        except Exception as exc:
            raise ComposioReadError("files_search", exc) from exc

    def _ms_stage_file_uploadable(self, file_path: str, tool_slug: str) -> dict[str, str]:
        """Stage a local file into Composio's object store for FileUploadable args."""
        from composio_files import stage_file_uploadable

        return stage_file_uploadable(
            file_path,
            tool_slug=tool_slug,
            toolkit_slug="one_drive",
            key_env=self.key_env,
        )

    @staticmethod
    def _ms_is_text_upload(path: Path) -> bool:
        """True when the file can be uploaded via CREATE_TEXT_FILE (plain text)."""
        if path.suffix.lower() in _MS_TEXT_UPLOAD_SUFFIXES:
            return True
        try:
            from composio_files import guess_mimetype
            mime = guess_mimetype(path)
        except Exception:
            mime = ""
        return bool(
            mime.startswith("text/")
            or mime in ("application/json", "application/xml", "application/javascript")
        )

    @guarded("drive.upload", target_arg="file_path", audit_provider="composio",
             audit_tool=lambda self: self._slug_for("files_upload"),
             tool_slug=lambda self: self._slug_for("files_upload"))
    def files_upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        if self.family == "microsoft":
            path = Path(file_path).expanduser()
            # Text → ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE (name+content[+folder]
            # over MCP; no Files API). Binary → ONE_DRIVE_ONEDRIVE_UPLOAD_FILE
            # with FileUploadable staging (project x-api-key) or source_url.
            # See https://composio.dev/toolkits/one_drive
            if self._ms_is_text_upload(path):
                slug = self._slug_for("files_upload")  # CREATE_TEXT_FILE
                try:
                    content = path.read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    raise RuntimeError(
                        f"file looks text-like but is not valid UTF-8 ({path.name}); "
                        "rename with a binary extension or provide a public source_url"
                    ) from exc
                args: dict[str, Any] = {"name": path.name, "content": content}
                if parent_id:
                    args["folder"] = parent_id
            else:
                slug = self._slug_for("files_upload_binary")
                file_arg: Any = self._ms_stage_file_uploadable(file_path, slug)
                args = {"file": file_arg}
                if parent_id:
                    args["folder"] = parent_id
            data = self._execute_composio_tool(slug, args, operation="files_upload")
            out = _ms_with_id(data)
            out["upload_slug"] = slug
            return out

        slug = self._slug_for("files_upload")
        args = {"file_path": file_path}
        if parent_id:
            args["parent_id"] = parent_id
        data = self._execute_composio_tool(slug, args, operation="files_upload")
        return data if isinstance(data, dict) else {}

    @guarded("drive.download", target_arg="file_id", audit_provider="composio",
             audit_tool=lambda self: self._slug_for("files_download"),
             tool_slug=lambda self: self._slug_for("files_download"))
    def files_download(self, file_id: str, output_path: str) -> dict[str, Any]:
        slug = self._slug_for("files_download")
        if self.family == "microsoft":
            # ONE_DRIVE_DOWNLOAD_FILE requires item_id + file_name (not output_path).
            args = {"item_id": file_id, "file_name": Path(output_path).name}
        else:
            args = {"file_id": file_id, "output_path": output_path}
        data = self._execute_composio_tool(slug, args, operation="files_download")
        payload = data if isinstance(data, dict) else {}
        if self.family == "microsoft":
            self._ms_persist_download(payload, output_path)
        return {"path": output_path, **payload}

    @staticmethod
    def _ms_persist_download(payload: Mapping[str, Any], output_path: str) -> None:
        """Write a Composio OneDrive download payload to ``output_path``.

        Prefer ``content.s3url`` (fetch via HTTPS). Fall back to inline
        bytes/base64 when present.
        """
        import base64

        from composio_files import download_s3url, find_s3url

        s3url = find_s3url(payload)
        if s3url:
            download_s3url(s3url, output_path)
            return

        content = payload.get("content")
        if content is None and isinstance(payload.get("data"), Mapping):
            content = payload["data"].get("content")
        if content is None:
            return
        try:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, (bytes, bytearray)):
                path.write_bytes(bytes(content))
            elif isinstance(content, Mapping):
                # Nested content without s3url already handled above; ignore.
                return
            elif isinstance(content, str):
                try:
                    path.write_bytes(base64.b64decode(content, validate=True))
                except Exception:
                    path.write_text(content, encoding="utf-8")
        except OSError:
            return

    @guarded("files.trash", target_arg="file_id", audit_provider="composio",
             audit_tool=lambda self: self._ms_cleanup_slug("files_trash"),
             tool_slug=lambda self: self._ms_cleanup_slug("files_trash"))
    def files_trash(self, file_id: str) -> dict[str, Any]:
        """Move a OneDrive item to the recycle bin (ONE_DRIVE_DELETE_ITEM).

        Uses the soft-delete slug, not ONE_DRIVE_DELETE_ITEM_PERMANENTLY.
        """
        self._require_microsoft_cleanup("files_trash")
        slug = self._slug_for("files_trash")
        self._execute_composio_tool(
            slug, {"item_id": file_id}, operation="files_trash",
        )
        return {"id": file_id, "reversible": True}

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
