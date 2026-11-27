#!/usr/bin/env python3
"""Google Workspace through Google's own remote MCP servers (provider ``google_mcp``).

Google's Workspace AI Tools Addendum (effective 2027-02-01) requires AI agents
reaching Workspace data to use the Google Workspace MCP servers or CLI. This
provider talks to the generally available Gmail, Calendar and Drive servers
directly (``google_mcp_transport``), as the delegate user of a service account
with domain-wide delegation — the same ``google:`` block and key routes as
``google_api``.

* **Auth** — one access token for ``gmail.modify``, ``calendar.events`` and
  ``drive``, held in memory only and refreshed in place. Never written to disk.
* **Writes** go through ``@guarded`` exactly like the other providers: the
  approval gate, single-use execution grants, payload-path checks and audit
  are unchanged (``audit_provider="google_mcp"``).
* **Google's approval prompt** — a call may come back ``input_required``. The
  client never answers it: it raises :class:`GoogleConfirmationRequired`
  unless the caller set ``confirmer`` (``confirmer(prompt) -> choice | None``),
  and refuses any choice the server did not offer. Under domain-wide
  delegation the server applies the same user-review policies as for a human.
* **Unsupported** — calendar cancel/uncancel (the tools have no status field),
  Drive untrash (no tool) and contacts (not on these servers).

Tool names and fields follow Google's Workspace MCP tool reference
(retrieved 2026-11-27). Nothing here has run against a real account yet.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

import google_mcp_transport as transport_mod  # noqa: E402
from google_mcp_transport import (  # noqa: E402,F401 - re-exported for callers
    AdminPolicyBlock,
    ConfirmationChoiceRefused,
    GoogleConfirmationRequired,
    GoogleMCPError,
    GoogleMCPTransport,
    GoogleMCPTransportError,
    MissingClientCapability,
    UserDeniedAction,
)
from providers import google_contacts  # noqa: E402
from providers.google_workspace import (  # noqa: E402
    _reject_crlf,
    _sa_credentials,
    _validated_attendee_emails,
    _validated_email,
)
from workspace_client import WorkspaceClient  # noqa: E402
from workspace_guardrails import guarded  # noqa: E402

PROVIDER = "google_mcp"

# The minimal scopes covering every mapped tool (Google's per-tool scope lists).
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive",
)

_SEARCH_PAGE_MAX = 50          # search_threads pageSize maximum
_CALENDAR_PAGE_SIZE = 100
_MAX_PAGES = 10
_SYSTEM_LABEL = re.compile(r"^[A-Z][A-Z_]*$")
_DRIVE_QUERY_SYNTAX = re.compile(r"(\b(contains|and|or|not|in)\b|!=|<=|>=|[=<>])")
_DRIVE_NAME_TERM = re.compile(r"\bname(\s*(?:contains|!=|=))")
_CALENDAR_UPDATE_FIELDS = {
    "title": "summary", "summary": "summary", "subject": "summary",
    "start": "startTime", "end": "endTime",
    "description": "description", "location": "location",
}


def _plugin_version() -> str:
    try:
        manifest = _PARENT.parent.parent / ".claude-plugin" / "plugin.json"
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version") or "0")
    except (OSError, ValueError):
        return "0"


def _audit_tool(tool: str) -> Callable[[Any], str]:
    return lambda _self: f"google_mcp:{tool}"


def _addresses(value: Any, kind: str) -> list[str]:
    """A comma/semicolon list or a list of addresses → validated addr-specs."""
    if value is None or value == "":
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).replace(";", ",").split(",")
    return [_validated_email(str(v), kind) for v in items if str(v).strip()]


def _local_time(value: str, default_clock: str) -> str:
    """Date-only → local wall time with no offset (the servers resolve the user's zone)."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("a start/end time is required")
    return text if "T" in text else f"{text}T{default_clock}"


def _iso_from_epoch_ms(raw: Any) -> str:
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return ""
    if ts > 1e12:
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return ""


def _records(data: Any, key: str) -> list[Mapping[str, Any]]:
    """``data[key]`` as a list of mappings (an empty ``{}`` means zero items)."""
    if not isinstance(data, Mapping):
        return []
    values = data.get(key)
    return [v for v in values if isinstance(v, Mapping)] if isinstance(values, list) else []


def _normalize_message(m: Mapping[str, Any], thread: Mapping[str, Any]) -> dict[str, Any]:
    thread_id = str(m.get("threadId") or thread.get("id") or "")
    sender = str(m.get("sender") or "") or "unknown"
    labels = [str(x) for x in m.get("labelIds") or []]
    out: dict[str, Any] = {
        "id": m.get("id"),
        "message_id": m.get("id"),
        "thread_id": thread_id,
        "threadId": thread_id,
        "sender": sender,
        "from": sender,
        "to": [str(x) for x in m.get("toRecipients") or []],
        "subject": str(m.get("subject") or "") or "(no subject)",
        "date": str(m.get("date") or "") or _iso_from_epoch_ms(m.get("internalDate")),
        "tags": labels,
        "labelIds": labels,
        "source": "gmail",
    }
    if m.get("ccRecipients"):
        out["cc"] = [str(x) for x in m["ccRecipients"]]
    if m.get("snippet") is not None:
        out["snippet"] = m.get("snippet")
    names = [n for n in m.get("addressDisplayNames") or [] if isinstance(n, Mapping)]
    for entry in names:
        if str(entry.get("emailAddress") or "").lower() == sender.lower() and entry.get("displayName"):
            out["sender_name"] = entry["displayName"]
            break
    if m.get("attachments") is not None or m.get("attachmentIds") is not None:
        out["has_attachments"] = bool(m.get("attachments") or m.get("attachmentIds"))
    link = m.get("viewUrl") or thread.get("viewUrl")
    if link:
        out["link"] = link
    return out


def _latest(messages: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    def key(m: Mapping[str, Any]) -> str:
        return str(m.get("date") or _iso_from_epoch_ms(m.get("internalDate")) or "")

    dated = [m for m in messages if key(m)]
    if dated:
        return max(dated, key=key)
    return messages[-1]


def _when(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("dateTime") or value.get("date") or "")
    return str(value or "")


def _normalize_event(e: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": e.get("id"),
        "title": str(e.get("summary") or "") or "(no title)",
        "summary": e.get("summary") or "",
        "start": _when(e.get("start")),
        "end": _when(e.get("end")),
        "attendees": [str(a.get("email")) for a in e.get("attendees") or []
                      if isinstance(a, Mapping) and a.get("email")],
        "source": "google_calendar",
    }
    organizer = e.get("organizer")
    if isinstance(organizer, Mapping) and organizer.get("email"):
        out["organizer"] = organizer["email"]
    if e.get("location"):
        out["location"] = e["location"]
    conference = e.get("conferenceUrl")
    if not conference and isinstance(e.get("conferenceData"), Mapping):
        for point in e["conferenceData"].get("entryPoints") or []:
            if isinstance(point, Mapping) and point.get("uri"):
                conference = point["uri"]
                break
    if conference:
        out["conference_link"] = conference
    if e.get("htmlLink"):
        out["event_link"] = e["htmlLink"]
        out["htmlLink"] = e["htmlLink"]
    if e.get("status"):
        out["status"] = e["status"]
    if e.get("description"):
        out["description"] = e["description"]
    return out


def _normalize_file(f: Mapping[str, Any]) -> dict[str, Any]:
    name = str(f.get("title") or f.get("name") or "")
    out: dict[str, Any] = {"id": f.get("id"), "name": name, "title": name}
    if f.get("mimeType"):
        out["mime_type"] = f["mimeType"]
        out["mimeType"] = f["mimeType"]
    if f.get("modifiedTime"):
        out["modified"] = f["modifiedTime"]
    if f.get("viewUrl"):
        out["link"] = f["viewUrl"]
    out["parents"] = [f["parentId"]] if f.get("parentId") else []
    out["source"] = "gdrive"
    return out


def _drive_query(query: str) -> str | None:
    """Neutral/Drive-v3 query → the search_files grammar.

    ``name`` is ``title`` there; free text becomes ``fullText contains '…'``.
    """
    text = str(query or "").strip()
    if not text:
        return None
    if _DRIVE_QUERY_SYNTAX.search(text) and ("'" in text or re.search(r"\b(true|false)\b", text)):
        return _DRIVE_NAME_TERM.sub(r"title\1", text)
    escaped = text.replace("\\", "\\\\").replace("'", "\\'")
    return f"fullText contains '{escaped}'"


class GoogleMCPWorkspaceClient(WorkspaceClient):
    """Gmail, Calendar and Drive over Google's Workspace MCP servers."""

    def __init__(self, config: Any, *, confirmer: Callable[[Any], Any] | None = None,
                 transport: GoogleMCPTransport | None = None) -> None:
        self.config = config
        self._provider_name = PROVIDER
        google = config.get("google", {}) if isinstance(config, Mapping) else {}
        google = google if isinstance(google, Mapping) else {}
        self.delegate_email = str(google.get("delegate_email") or "").strip()
        # Called with a GoogleConfirmationRequired; returns one of its choice
        # ids or None. Only the caller sets this — the client never answers.
        self.confirmer = confirmer
        self._credentials: Any = None      # in memory only
        self._transport = transport or GoogleMCPTransport(self._access_token, client_version=_plugin_version())

    # ── identity and auth ───────────────────────────────────────────────

    def acting_identity(self, family: str) -> str | None:
        """Domain-wide delegation impersonates delegate_email for every product."""
        return self.delegate_email or None

    def _access_token(self) -> str:
        creds = self._credentials
        if creds is None:
            sa_path, _delegate = google_contacts.service_account_settings(self.config)
            if not sa_path:
                raise RuntimeError(
                    "google_mcp requires google.service_account_path (or the "
                    "GOOGLE_SERVICE_ACCOUNT_JSON secret) for a service account with "
                    "domain-wide delegation"
                )
            creds = _sa_credentials(service_account_path=sa_path, delegate_email=self.delegate_email,
                                    scopes=list(SCOPES))
            self._credentials = creds
        elif not getattr(creds, "valid", False):
            from google.auth.transport.requests import Request

            creds.refresh(Request())
        return str(creds.token)

    # ── plumbing ────────────────────────────────────────────────────────

    def _call(self, server: str, tool: str, arguments: Mapping[str, Any], *, read_only: bool) -> Any:
        return transport_mod.call_tool_confirmed(
            self._transport, server, tool, arguments, read_only=read_only, confirmer=self.confirmer)

    @staticmethod
    def _reject_draft_id(message_id: str) -> str:
        mid = str(message_id or "").strip()
        if not mid:
            raise ValueError("a message id is required")
        if re.match(r"^r-?\d+$", mid):
            raise ValueError(f"{mid!r} is a draft id, not a message id")
        return mid

    def _label_id(self, name_or_id: str) -> str:
        token = str(name_or_id or "").strip()
        if not token:
            raise ValueError("a label id or name is required")
        if token.startswith("Label_") or _SYSTEM_LABEL.match(token):
            return token
        for label in self.mail_list_tags():
            if token.lower() in (str(label.get("name") or "").lower(), str(label.get("id") or "").lower()):
                return str(label["id"])
        raise ValueError(f"no Gmail label named {token!r} (create it first, or pass its Label_ id)")

    def _modify_labels(self, message_id: str, *, add: list[str] | None = None,
                       remove: list[str] | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"messageId": self._reject_draft_id(message_id)}
        if add:
            args["addLabelIds"] = list(add)
        if remove:
            args["removeLabelIds"] = list(remove)
        data = self._call("gmail", "update_message_labels", args, read_only=False)
        out: dict[str, Any] = {"id": args["messageId"], "add_label_ids": list(add or []),
                               "remove_label_ids": list(remove or [])}
        if isinstance(data, Mapping) and isinstance(data.get("labelIds"), list):
            out["label_ids"] = list(data["labelIds"])
        return out

    # ── mail reads ──────────────────────────────────────────────────────

    def mail_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        """One record per matching thread: its latest message, normalized."""
        wanted = max(int(max_results or 0), 1)
        out: list[dict[str, Any]] = []
        token: str | None = None
        for _page in range(_MAX_PAGES):
            args: dict[str, Any] = {}
            if str(query or "").strip():
                args["query"] = str(query)
            args["pageSize"] = min(wanted - len(out), _SEARCH_PAGE_MAX)
            if token:
                args["pageToken"] = token
            data = self._call("gmail", "search_threads", args, read_only=True)
            for thread in _records(data, "threads"):
                messages = [m for m in thread.get("messages") or [] if isinstance(m, Mapping)]
                if not messages and thread.get("id"):
                    full = self._call("gmail", "get_thread",
                                      {"threadId": str(thread["id"]), "messageFormat": "MINIMAL"},
                                      read_only=True)
                    messages = _records(full, "messages")
                if not messages:
                    continue
                out.append(_normalize_message(_latest(messages), thread))
                if len(out) >= wanted:
                    return out
            token = data.get("nextPageToken") if isinstance(data, Mapping) else None
            if not token:
                break
        return out

    def mail_list_tags(self) -> list[dict[str, Any]]:
        data = self._call("gmail", "list_labels", {}, read_only=True)
        out: list[dict[str, Any]] = []
        for label in _records(data, "labels"):
            lid = str(label.get("labelId") or label.get("id") or "")
            name = str(label.get("name") or "")
            if not lid and not name:
                continue
            kind = str(label.get("labelType") or "user").lower().replace("label_type_", "")
            out.append({"id": lid or name, "name": name or lid, "type": kind or "user"})
        return out

    def mail_list_folders(self, include_hidden: bool = False,
                          max_results: int = 100) -> list[dict[str, Any]]:
        """Gmail has labels, not folders: the same records as ``mail_list_tags``."""
        return self.mail_list_tags()[: max(int(max_results or 0), 1)]

    # ── mail writes ─────────────────────────────────────────────────────

    def _message_args(self, to: str, subject: str, body: str, cc: str | None) -> dict[str, Any]:
        recipients = _addresses(to, "recipient")
        if not recipients:
            raise ValueError("at least one recipient is required")
        args: dict[str, Any] = {"to": recipients}
        copies = _addresses(cc, "cc address")
        if copies:
            args["cc"] = copies
        args["subject"] = _reject_crlf(subject or "", "subject")
        args["body"] = str(body or "")
        return args

    @guarded("mail.send", target_arg="to", audit_provider=PROVIDER, audit_tool=_audit_tool("send_message"),
             tool_slug="send_message",
             block_error="cancelled by guardrail (requires CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1)")
    def mail_send(self, to: str, subject: str, body: str, cc: str | None = None) -> dict[str, Any]:
        data = self._call("gmail", "send_message", self._message_args(to, subject, body, cc), read_only=False)
        data = data if isinstance(data, Mapping) else {}
        return {"id": data.get("id"), "message_id": data.get("id"), "thread_id": data.get("threadId"),
                "link": data.get("viewUrl")}

    @guarded("mail.draft", target_arg="to", audit_provider=PROVIDER, audit_tool=_audit_tool("create_draft"),
             tool_slug="create_draft")
    def mail_create_draft(self, to: str, subject: str, body: str, cc: str | None = None) -> dict[str, Any]:
        """Surfaces the message id as ``id`` when the server returns one (keeps ``draft_id``)."""
        data = self._call("gmail", "create_draft", self._message_args(to, subject, body, cc), read_only=False)
        data = data if isinstance(data, Mapping) else {}
        draft_id = data.get("id")
        message_id = data.get("messageId")
        return {"id": message_id or draft_id, "draft_id": draft_id, "message_id": message_id,
                "thread_id": data.get("threadId"), "link": data.get("viewUrl")}

    @guarded("mail.tag", target_arg="message_id", audit_provider=PROVIDER, audit_tool=_audit_tool("label_message"),
             tool_slug="label_message")
    def mail_tag(self, message_id: str, tag_id: str) -> dict[str, Any]:
        mid = self._reject_draft_id(message_id)
        label_id = self._label_id(tag_id)
        self._call("gmail", "label_message", {"messageId": mid, "labelIds": [label_id]}, read_only=False)
        return {"id": mid, "label_id": label_id, "reversible": True}

    @guarded("mail.create_tag", target_arg="name", audit_provider=PROVIDER, audit_tool=_audit_tool("create_label"),
             tool_slug="create_label")
    def mail_create_tag(self, name: str) -> dict[str, Any]:
        label = _reject_crlf(str(name or "").strip(), "label name")
        if not label:
            raise ValueError("a label name is required")
        data = self._call("gmail", "create_label", {"displayName": label}, read_only=False)
        data = data if isinstance(data, Mapping) else {}
        return {"id": data.get("labelId") or data.get("id"), "name": data.get("name") or label}

    @guarded("mail.archive", target_arg="message_id", audit_provider=PROVIDER,
             audit_tool=_audit_tool("update_message_labels"), tool_slug="update_message_labels")
    def mail_archive(self, message_id: str) -> dict[str, Any]:
        out = self._modify_labels(message_id, remove=["INBOX"])
        return {**out, "destination": "archive", "restore_target": out["id"], "reversible": True}

    @guarded("mail.unarchive", target_arg="message_id", audit_provider=PROVIDER,
             audit_tool=_audit_tool("update_message_labels"), tool_slug="update_message_labels")
    def mail_unarchive(self, message_id: str) -> dict[str, Any]:
        out = self._modify_labels(message_id, add=["INBOX"])
        return {**out, "destination": "inbox", "restore_target": out["id"], "reversible": True}

    @guarded("mail.move", target_arg="message_id", audit_provider=PROVIDER,
             audit_tool=_audit_tool("update_message_labels"), tool_slug="update_message_labels")
    def mail_move_to_folder(self, message_id: str, folder_id: str) -> dict[str, Any]:
        """Gmail-style move: add the destination label and take the message out of INBOX."""
        token = str(folder_id or "").strip()
        lower = token.lower()
        if lower == "archive":
            add, remove, destination = [], ["INBOX"], "archive"
        elif lower == "inbox":
            add, remove, destination = ["INBOX"], [], "inbox"
        else:
            label_id = self._label_id(token)
            add, remove, destination = [label_id], ([] if label_id == "INBOX" else ["INBOX"]), label_id
        out = self._modify_labels(message_id, add=add, remove=remove)
        return {**out, "destination": destination, "undo_add_label_ids": list(remove),
                "undo_remove_label_ids": list(add), "restore_target": out["id"], "reversible": True}

    @guarded("mail.trash", target_arg="message_id", audit_provider=PROVIDER,
             audit_tool=_audit_tool("trash_message"), tool_slug="trash_message")
    def mail_trash(self, message_id: str) -> dict[str, Any]:
        mid = self._reject_draft_id(message_id)
        self._call("gmail", "trash_message", {"messageId": mid}, read_only=False)
        return {"id": mid, "destination": "trash", "restore_target": mid, "reversible": True}

    @guarded("mail.untrash", target_arg="message_id", audit_provider=PROVIDER,
             audit_tool=_audit_tool("untrash_message"), tool_slug="untrash_message")
    def mail_untrash(self, message_id: str) -> dict[str, Any]:
        """Out of Trash. Google's tool does not put it back in the Inbox; unarchive does."""
        mid = self._reject_draft_id(message_id)
        self._call("gmail", "untrash_message", {"messageId": mid}, read_only=False)
        return {"id": mid, "restore_target": mid, "reversible": True}

    # ── calendar ────────────────────────────────────────────────────────

    def calendar_list(self, start: str, end: str) -> list[dict[str, Any]]:
        base: dict[str, Any] = {
            "calendarId": "primary",
            "startTime": _local_time(start, "00:00:00"),
            "endTime": _local_time(end, "23:59:59"),
            "pageSize": _CALENDAR_PAGE_SIZE,
        }
        out: list[dict[str, Any]] = []
        token: str | None = None
        for _page in range(_MAX_PAGES):
            args = dict(base, pageToken=token) if token else dict(base)
            data = self._call("calendar", "list_events", args, read_only=True)
            out.extend(_normalize_event(e) for e in _records(data, "events"))
            token = data.get("nextPageToken") if isinstance(data, Mapping) else None
            if not token:
                break
        return out

    @staticmethod
    def _event_result(data: Any, fallback_id: str = "") -> dict[str, Any]:
        data = data if isinstance(data, Mapping) else {}
        out = {"id": data.get("id") or fallback_id, "htmlLink": data.get("htmlLink"),
               "status": data.get("status")}
        if data.get("conferenceUrl"):
            out["conference_link"] = data["conferenceUrl"]
            out["hangoutLink"] = data["conferenceUrl"]
        return out

    @guarded("calendar.create", target_arg="title", audit_provider=PROVIDER,
             audit_tool=_audit_tool("create_event"), tool_slug="create_event")
    def calendar_create(self, title: str, start: str, end: str,
                        attendees: list[str] | None = None,
                        description: str | None = None) -> dict[str, Any]:
        """Create an event with a Meet link (Meet-by-default, like google_api)."""
        args: dict[str, Any] = {
            "calendarId": "primary",
            "summary": _reject_crlf(title, "title"),
            "startTime": _local_time(start, "10:00:00"),
            "endTime": _local_time(end, "11:00:00"),
        }
        guests = _validated_attendee_emails(attendees)
        if guests:
            args["attendees"] = [{"email": a} for a in guests]
        if description:
            args["description"] = str(description)
        args["addGoogleMeetUrl"] = True
        return self._event_result(self._call("calendar", "create_event", args, read_only=False))

    @guarded("calendar.update", target_arg="event_id", audit_provider=PROVIDER,
             audit_tool=_audit_tool("update_event"), tool_slug="update_event")
    def calendar_update(self, event_id: str, **fields: Any) -> dict[str, Any]:
        """Update title/start/end/description/location, or add attendees.

        ``attendees`` ADDS guests (update_event has no replace-all field);
        ``removed_attendees`` removes them. Any other field is refused, so a
        typo or an unsupported change (``status``) never silently no-ops.
        """
        if not str(event_id or "").strip():
            raise ValueError("calendar.update requires event_id")
        args: dict[str, Any] = {"calendarId": "primary", "eventId": str(event_id)}
        unknown = sorted(k for k in fields if k not in _CALENDAR_UPDATE_FIELDS
                         and k not in ("attendees", "removed_attendees"))
        if unknown:
            raise ValueError(
                f"calendar.update on google_mcp does not support: {', '.join(unknown)} "
                "(supported: title, start, end, description, location, attendees, removed_attendees)")
        for key, value in fields.items():
            if value is None:
                continue
            if key in ("start", "end"):
                args[_CALENDAR_UPDATE_FIELDS[key]] = _local_time(str(value), "10:00:00" if key == "start" else "11:00:00")
            elif key == "attendees":
                args["addedAttendees"] = [{"email": a} for a in _validated_attendee_emails(list(value))]
            elif key == "removed_attendees":
                args["removedAttendeeEmails"] = _validated_attendee_emails(list(value))
            elif key in ("title", "summary", "subject"):
                args["summary"] = _reject_crlf(str(value), "title")
            else:
                args[_CALENDAR_UPDATE_FIELDS[key]] = str(value)
        if len(args) == 2:
            raise ValueError("calendar.update requires at least one field to change")
        return self._event_result(self._call("calendar", "update_event", args, read_only=False), str(event_id))

    @guarded("calendar.delete", target_arg="event_id", audit_provider=PROVIDER,
             audit_tool=_audit_tool("delete_event"), tool_slug="delete_event",
             block_error="cancelled by guardrail (requires CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1)")
    def calendar_delete(self, event_id: str) -> dict[str, Any]:
        """Delete an event. Destructive: there is no restore tool."""
        eid = str(event_id or "").strip()
        if not eid:
            raise ValueError("calendar.delete requires event_id")
        self._call("calendar", "delete_event", {"calendarId": "primary", "eventId": eid}, read_only=False)
        return {"id": eid, "deleted": True}

    def calendar_cancel(self, event_id: str) -> dict[str, Any]:
        raise NotImplementedError("google_mcp cannot cancel an event: update_event has no status field")

    def calendar_uncancel(self, event_id: str) -> dict[str, Any]:
        raise NotImplementedError("google_mcp cannot uncancel an event: update_event has no status field")

    # ── files ───────────────────────────────────────────────────────────

    def files_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        args: dict[str, Any] = {}
        compiled = _drive_query(query)
        if compiled:
            args["query"] = compiled
        args["pageSize"] = max(int(max_results or 0), 1)
        args["excludeContentSnippets"] = True
        data = self._call("drive", "search_files", args, read_only=True)
        return [_normalize_file(f) for f in _records(data, "files")][: args["pageSize"]]

    @guarded("files.upload", target_arg="file_path", audit_provider=PROVIDER,
             audit_tool=_audit_tool("create_file"), tool_slug="create_file")
    def files_upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        """Upload bytes as ``base64Content`` and keep the type (no Google-format conversion)."""
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"file not found: {file_path}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        args: dict[str, Any] = {
            "title": path.name,
            "contentMimeType": mime,
            "base64Content": base64.b64encode(path.read_bytes()).decode("ascii"),
            "disableConversionToGoogleType": True,
        }
        if parent_id:
            args["parentId"] = str(parent_id)
        data = self._call("drive", "create_file", args, read_only=False)
        data = data if isinstance(data, Mapping) else {}
        return {"id": data.get("id"), "name": data.get("title") or path.name,
                "mime_type": data.get("mimeType") or mime, "link": data.get("viewUrl"),
                "parent_id": data.get("parentId") or parent_id}

    @guarded("files.download", target_arg="file_id", audit_provider=PROVIDER,
             audit_tool=_audit_tool("download_file_content"), tool_slug="download_file_content")
    def files_download(self, file_id: str, output_path: str) -> dict[str, Any]:
        """Decode the base64 ``content`` and write it to ``output_path`` (never echoed back)."""
        data = self._call("drive", "download_file_content", {"fileId": str(file_id)}, read_only=True)
        data = data if isinstance(data, Mapping) else {}
        encoded = data.get("content")
        if not isinstance(encoded, str):
            raise GoogleMCPError(f"download_file_content returned no content for {file_id}",
                                 server="drive", tool="download_file_content")
        try:
            raw = base64.b64decode(encoded, validate=False)
        except (ValueError, TypeError) as exc:
            raise GoogleMCPError("download_file_content returned content that is not base64",
                                 server="drive", tool="download_file_content") from exc
        dest = Path(output_path).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "xb") as fh:      # a download never overwrites a file
            fh.write(raw)
        return {"path": str(output_path), "id": data.get("id") or file_id, "name": data.get("title"),
                "mime_type": data.get("mimeType"), "bytes": len(raw)}

    @guarded("files.trash", target_arg="file_id", audit_provider=PROVIDER,
             audit_tool=_audit_tool("trash_file"), tool_slug="trash_file")
    def files_trash(self, file_id: str) -> dict[str, Any]:
        """Drive trash (30-day recoverable). google_mcp has no untrash tool: restore in Drive."""
        fid = str(file_id or "").strip()
        if not fid:
            raise ValueError("files.trash requires file_id")
        self._call("drive", "trash_file", {"fileId": fid}, read_only=False)
        return {"id": fid, "trashed": True, "reversible": True, "restore_target": fid,
                "restore_hint": "restore from Google Drive Trash (google_mcp has no untrash tool)"}

    # ── health ──────────────────────────────────────────────────────────

    def health_check(self) -> bool:
        try:
            self.mail_list_tags()
            return True
        except Exception:  # noqa: BLE001 - a health check reports, never raises
            return False


def get_google_mcp_client(config: Any) -> GoogleMCPWorkspaceClient:
    return GoogleMCPWorkspaceClient(config)
