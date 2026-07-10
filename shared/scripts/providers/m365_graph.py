#!/usr/bin/env python3
"""Microsoft 365 (Graph API) backend for WorkspaceClient.

Implements the provider-neutral WorkspaceClient surface (mail_*, calendar_*,
files_*) over the Microsoft Graph REST API v1.0 using ``requests`` directly —
no Graph SDK.  Authentication uses ``msal``, imported LAZILY inside the auth
path so this module imports fine even when msal is not installed; a clear
RuntimeError naming ``pip install msal`` is raised only when a token is actually
requested.

Config (under the top-level ``m365`` key, with
``integrations.workspace.provider: m365``):

    m365:
      tenant_id: "<entra-tenant-guid>"
      client_id: "<app-registration-client-id>"
      client_secret_env: "M365_CLIENT_SECRET"   # env var holding the secret (default)
      auth: "client_credentials"                # or "device_code"
      user_principal: "user@tenant.com"         # mailbox UPN, REQUIRED for client_credentials
      token_cache_path: "~/.hermes/secrets/m365-token-cache.json"  # optional

Auth modes:
  * ``client_credentials`` (default) -> msal.ConfidentialClientApplication,
    scope ``["https://graph.microsoft.com/.default"]``, endpoints operate on
    ``/users/{user_principal}/...``.
  * ``device_code`` -> msal.PublicClientApplication with the device flow message
    printed to stderr, endpoints operate on ``/me/...``.

All HTTP is routed through :meth:`_request` and all token acquisition through
:meth:`_get_token` so both can be monkeypatched in unit tests (no network, no
msal required).  Non-2xx responses raise RuntimeError carrying the Graph status
and error message; the ``@guarded`` wrapper converts that into an audited
failure ActionResult.  Read methods (mail_search, calendar_list, files_search,
mail_list_tags) follow the Google provider's pattern: warn + return ``[]`` on
failure.

Immutable ids:
  Every Graph request sent through :meth:`_request` carries the
  ``Prefer: IdType="ImmutableId"`` header.  By default Graph message ids CHANGE
  when a message moves folders (archive/trash); requesting immutable ids keeps
  them stable across moves so the generic soft-delete restore flow (which
  restores by the original/persisted id) still resolves after a move.  As a
  belt-and-braces measure, :meth:`mail_archive`/:meth:`mail_trash` also return a
  ``restore_target`` key (the post-move id) in their ActionResult data.

Provider differences vs Google/Composio:
  * Drafts ARE supported (POST /messages).
  * Sending is destructive and env-gated identically to gmail.send.
  * There is no calendar "uncancel" — Graph cannot reinstate a cancelled event.
    Because there is no restore path, ``calendar.cancel`` is NOT supported for
    m365: the capability is False (see workspace_capabilities.py) and
    :meth:`calendar_cancel` returns a failure ActionResult explaining to cancel
    via Outlook or delete+recreate the event (it does not raise).
    :meth:`calendar_uncancel` likewise raises NotImplementedError.
  * Change-notification webhooks are intentionally deferred — polling only.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path
from typing import Any, Mapping

import requests

_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from workspace_client import WorkspaceClient
from workspace_guardrails import guarded
from query_compiler import compile_query

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_SCOPE = ["https://graph.microsoft.com/.default"]
DEVICE_SCOPE = [
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/Calendars.ReadWrite",
    "https://graph.microsoft.com/Files.ReadWrite.All",
    "https://graph.microsoft.com/User.Read",
]


# Neutral mail.*/files.* guardrail action ids used by this provider
# (mail.send destructive; mail.draft / calendar.create|update / files.upload|download
# safe writes; archive/trash/tag/cancel ungated, mirroring the Google provider)
# are classified directly in workspace_guardrails.py WRITE/DESTRUCTIVE/SAFE sets.


def _split_addrs(value: str | None) -> list[str]:
    if not value:
        return []
    return [a.strip() for a in str(value).split(",") if a.strip()]


def _recipients(value: str | None) -> list[dict[str, Any]]:
    return [{"emailAddress": {"address": a}} for a in _split_addrs(value)]


def _graph_datetime(value: str, default_time: str = "10:00:00") -> str:
    """Normalise a date/datetime into a Graph dateTime (no trailing Z).

    Graph's event dateTime is paired with a separate timeZone, so a trailing
    ``Z`` must be stripped.  Bare dates are padded with ``default_time``.
    """
    v = str(value)
    if "T" not in v:
        v = f"{v}T{default_time}"
    if v.endswith("Z"):
        v = v[:-1]
    return v


class M365GraphClient(WorkspaceClient):
    """Microsoft 365 provider over Graph REST v1.0."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self._provider_name = "m365"
        self.base_url = GRAPH_BASE

        m365 = config.get("m365", {}) if isinstance(config, Mapping) else {}
        if not isinstance(m365, Mapping):
            m365 = {}
        self.tenant_id = str(m365.get("tenant_id", "") or "")
        self.client_id = str(m365.get("client_id", "") or "")
        self.client_secret_env = str(m365.get("client_secret_env", "M365_CLIENT_SECRET") or "M365_CLIENT_SECRET")
        self.auth_mode = str(m365.get("auth", "client_credentials") or "client_credentials")
        self.user_principal = str(m365.get("user_principal", "") or "")
        tcp = m365.get("token_cache_path")
        self.token_cache_path = str(tcp) if tcp else ""

        self._token: str | None = None
        self._msal_app = None

    # ── User base path ────────────────────────────────────────────────

    def _user_base(self) -> str:
        """Return the Graph path prefix for the active identity."""
        if self.auth_mode == "device_code":
            return "/me"
        if not self.user_principal:
            raise RuntimeError(
                "m365 client_credentials auth requires m365.user_principal "
                "(the mailbox UPN, e.g. user@tenant.com)"
            )
        return f"/users/{self.user_principal}"

    # ── Auth (msal imported lazily) ───────────────────────────────────

    def _get_token(self) -> str:
        """Acquire a Graph access token. Raises RuntimeError on any failure."""
        if self._token:
            return self._token
        try:
            import msal  # noqa: F401  — lazy: import only when auth is attempted
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
            raise RuntimeError(
                "Microsoft 365 provider requires the 'msal' package. "
                "Install it with: pip install msal"
            ) from exc

        import os

        authority = f"https://login.microsoftonline.com/{self.tenant_id}"

        if self.auth_mode == "device_code":
            app = msal.PublicClientApplication(self.client_id, authority=authority)
            flow = app.initiate_device_flow(scopes=DEVICE_SCOPE)
            if "user_code" not in flow:
                raise RuntimeError(f"Failed to start device flow: {flow.get('error_description', flow)}")
            print(flow.get("message", "Complete device sign-in in your browser."), file=sys.stderr)
            result = app.acquire_token_by_device_flow(flow)
        else:
            secret = os.getenv(self.client_secret_env, "")
            if not secret:
                raise RuntimeError(
                    f"m365 client_credentials auth requires the client secret in "
                    f"env var {self.client_secret_env}"
                )
            if not self.tenant_id or not self.client_id:
                raise RuntimeError("m365 config requires tenant_id and client_id")
            app = msal.ConfidentialClientApplication(
                self.client_id, authority=authority, client_credential=secret
            )
            result = app.acquire_token_for_client(scopes=DEFAULT_SCOPE)

        token = result.get("access_token") if isinstance(result, Mapping) else None
        if not token:
            err = result.get("error_description") if isinstance(result, Mapping) else result
            raise RuntimeError(f"Failed to acquire Microsoft Graph token: {err}")
        self._token = token
        return token

    # ── HTTP (single seam) ────────────────────────────────────────────

    @staticmethod
    def _error_message(resp: "requests.Response") -> str:
        try:
            payload = resp.json()
            err = payload.get("error", {}) if isinstance(payload, Mapping) else {}
            msg = err.get("message") if isinstance(err, Mapping) else None
        except Exception:
            msg = None
        return f"Graph API {resp.status_code}: {msg or (resp.text or '').strip()[:300]}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        raw: bool = False,
        timeout: int = 30,
    ) -> Any:
        """Perform one Graph request. Returns parsed JSON dict (or raw bytes if
        ``raw=True``). Raises RuntimeError on non-2xx responses."""
        token = self._get_token()
        url = f"{self.base_url}{path}"
        # Prefer immutable ids on EVERY Graph request. By default Graph message
        # ids change when a message moves folders (archive/trash), which would
        # break the generic restore flow that restores by the original target id.
        # Requesting IdType="ImmutableId" makes ids stable across moves so a
        # persisted id/restore_target still resolves after archive/trash.
        hdrs = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Prefer": 'IdType="ImmutableId"',
        }
        if headers:
            hdrs.update(headers)
        resp = requests.request(
            method, url, params=params, json=json_body, data=content,
            headers=hdrs, timeout=timeout,
        )
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(self._error_message(resp))
        if raw:
            return resp.content
        if resp.status_code == 204 or not (resp.content or b""):
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # ── Normalisers ───────────────────────────────────────────────────

    @staticmethod
    def _normalize_message(m: Mapping[str, Any]) -> dict[str, Any]:
        sender = ""
        frm = m.get("from") or {}
        if isinstance(frm, Mapping):
            addr = frm.get("emailAddress", {}) or {}
            sender = addr.get("address", "") or addr.get("name", "") or ""
        out: dict[str, Any] = {
            "id": m.get("id"),
            # schemas.validate_message requires non-empty sender/subject/date;
            # Graph drafts and some system notifications omit "from", subjects
            # can be blank, and drafts have no receivedDateTime.
            "sender": sender or "unknown",
            "subject": m.get("subject") or "(no subject)",
            "date": m.get("receivedDateTime") or m.get("sentDateTime") or m.get("createdDateTime") or "",
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

    @staticmethod
    def _normalize_event(e: Mapping[str, Any]) -> dict[str, Any]:
        start = (e.get("start", {}) or {}).get("dateTime")
        end = (e.get("end", {}) or {}).get("dateTime")
        attendees = []
        for a in e.get("attendees", []) or []:
            addr = ((a.get("emailAddress", {}) or {}).get("address")) if isinstance(a, Mapping) else None
            if addr:
                attendees.append(addr)
        organizer = ((e.get("organizer", {}) or {}).get("emailAddress", {}) or {}).get("address")
        location = (e.get("location", {}) or {}).get("displayName")
        conference_link = ((e.get("onlineMeeting", {}) or {}).get("joinUrl")) or location
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

    @staticmethod
    def _normalize_file(f: Mapping[str, Any]) -> dict[str, Any]:
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

    # ── Mail: reads ───────────────────────────────────────────────────

    def mail_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        try:
            compiled = compile_query(query, "m365")
            params: dict[str, Any] = {"$top": max_results}
            if compiled.get("filter"):
                params["$filter"] = compiled["filter"]
            if compiled.get("search"):
                params["$search"] = f'"{compiled["search"]}"'
            # Folder scope is carried out-of-band: Graph scopes folders by URL
            # path (/mailFolders/{well-known-or-id}/messages), NOT by $filter.
            folder = compiled.get("folder")
            if folder:
                path = f"{self._user_base()}/mailFolders/{folder}/messages"
            else:
                path = f"{self._user_base()}/messages"
            data = self._request("GET", path, params=params)
            value = data.get("value", []) if isinstance(data, Mapping) else []
            return [self._normalize_message(m) for m in value if isinstance(m, Mapping)]
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"m365 mail_search failed: {exc}")
            return []

    def mail_list_tags(self) -> list[dict[str, Any]]:
        """List Outlook master categories. Read-only. For m365 the tag id IS the
        category displayName."""
        try:
            data = self._request("GET", f"{self._user_base()}/outlook/masterCategories")
            value = data.get("value", []) if isinstance(data, Mapping) else []
            out = []
            for c in value:
                if not isinstance(c, Mapping):
                    continue
                name = c.get("displayName", "")
                out.append({
                    "id": name,                # tag id == displayName for m365
                    "name": name,
                    "displayName": name,
                    "color": c.get("color"),
                    "graph_id": c.get("id"),
                })
            return out
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"m365 mail_list_tags failed: {exc}")
            return []

    # ── Mail: writes ──────────────────────────────────────────────────

    @guarded("mail.draft", target_arg="to", audit_provider="m365", audit_tool="graph_api")
    def mail_create_draft(self, to: str, subject: str, body: str,
                          cc: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": _recipients(to),
        }
        if cc:
            payload["ccRecipients"] = _recipients(cc)
        return self._request("POST", f"{self._user_base()}/messages", json_body=payload)

    @guarded("mail.send", target_arg="to", audit_provider="m365", audit_tool="graph_api",
             block_error="cancelled by guardrail (requires CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1)")
    def mail_send(self, to: str, subject: str, body: str,
                  cc: str | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": _recipients(to),
        }
        if cc:
            message["ccRecipients"] = _recipients(cc)
        self._request("POST", f"{self._user_base()}/sendMail",
                      json_body={"message": message, "saveToSentItems": True})
        return {"status": "sent", "to": to}

    def _move(self, message_id: str, destination: str) -> dict[str, Any]:
        return self._request(
            "POST", f"{self._user_base()}/messages/{message_id}/move",
            json_body={"destinationId": destination},
        )

    @guarded("mail.archive", target_arg="message_id", audit_provider="m365", audit_tool="graph_api")
    def mail_archive(self, message_id: str) -> dict[str, Any]:
        data = self._move(message_id, "archive")
        # restore_target: the post-move id. With immutable ids this equals the
        # original id, but the generic restore flow prefers this persisted value
        # over the original action target for correctness across moves.
        moved_id = data.get("id", message_id)
        return {"id": moved_id, "destination": "archive", "restore_target": moved_id}

    @guarded("mail.unarchive", target_arg="message_id", audit_provider="m365", audit_tool="graph_api")
    def mail_unarchive(self, message_id: str) -> dict[str, Any]:
        data = self._move(message_id, "inbox")
        return {"id": data.get("id", message_id), "destination": "inbox"}

    @guarded("mail.trash", target_arg="message_id", audit_provider="m365", audit_tool="graph_api")
    def mail_trash(self, message_id: str) -> dict[str, Any]:
        data = self._move(message_id, "deleteditems")
        moved_id = data.get("id", message_id)
        return {"id": moved_id, "destination": "deleteditems", "reversible": True,
                "restore_target": moved_id}

    @guarded("mail.untrash", target_arg="message_id", audit_provider="m365", audit_tool="graph_api")
    def mail_untrash(self, message_id: str) -> dict[str, Any]:
        data = self._move(message_id, "inbox")
        return {"id": data.get("id", message_id), "destination": "inbox"}

    @guarded("mail.tag", target_arg="message_id", audit_provider="m365", audit_tool="graph_api")
    def mail_tag(self, message_id: str, tag_id: str) -> dict[str, Any]:
        """Apply an Outlook category. For m365 the tag_id IS the category
        displayName. Fetches current categories and appends."""
        current = self._request(
            "GET", f"{self._user_base()}/messages/{message_id}",
            params={"$select": "categories"},
        )
        existing = list(current.get("categories", []) or []) if isinstance(current, Mapping) else []
        if tag_id not in existing:
            existing.append(tag_id)
        self._request(
            "PATCH", f"{self._user_base()}/messages/{message_id}",
            json_body={"categories": existing},
        )
        return {"id": message_id, "categories": existing}

    @guarded("mail.create_tag", target_arg="name", audit_provider="m365", audit_tool="graph_api")
    def mail_create_tag(self, name: str) -> dict[str, Any]:
        data = self._request(
            "POST", f"{self._user_base()}/outlook/masterCategories",
            json_body={"displayName": name, "color": "preset0"},
        )
        return {"id": name, "name": name, "graph_id": data.get("id") if isinstance(data, Mapping) else None}

    # ── Calendar ──────────────────────────────────────────────────────

    def calendar_list(self, start: str, end: str) -> list[dict[str, Any]]:
        if "T" not in start:
            start = f"{start}T00:00:00Z"
        if "T" not in end:
            end = f"{end}T23:59:59Z"
        try:
            params = {"startDateTime": start, "endDateTime": end, "$top": 100}
            data = self._request("GET", f"{self._user_base()}/calendarView", params=params)
            value = data.get("value", []) if isinstance(data, Mapping) else []
            return [self._normalize_event(e) for e in value if isinstance(e, Mapping)]
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"m365 calendar_list failed: {exc}")
            return []

    @guarded("calendar.create", target_arg="title", audit_provider="m365", audit_tool="graph_api")
    def calendar_create(self, title: str, start: str, end: str,
                        attendees: list[str] | None = None,
                        description: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subject": title,
            "start": {"dateTime": _graph_datetime(start, "10:00:00"), "timeZone": "UTC"},
            "end": {"dateTime": _graph_datetime(end, "11:00:00"), "timeZone": "UTC"},
        }
        if attendees:
            payload["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"} for a in attendees
            ]
        if description:
            payload["body"] = {"contentType": "Text", "content": description}
        return self._request("POST", f"{self._user_base()}/events", json_body=payload)

    @guarded("calendar.update", target_arg="event_id", audit_provider="m365", audit_tool="graph_api")
    def calendar_update(self, event_id: str, **fields: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in fields.items():
            if key in ("title", "summary", "subject"):
                payload["subject"] = value
            elif key == "start":
                payload["start"] = {"dateTime": _graph_datetime(str(value), "10:00:00"), "timeZone": "UTC"}
            elif key == "end":
                payload["end"] = {"dateTime": _graph_datetime(str(value), "11:00:00"), "timeZone": "UTC"}
            elif key == "description":
                payload["body"] = {"contentType": "Text", "content": value}
            elif key == "location":
                payload["location"] = {"displayName": value}
            else:
                payload[key] = value
        return self._request("PATCH", f"{self._user_base()}/events/{event_id}", json_body=payload)

    def calendar_cancel(self, event_id: str) -> dict[str, Any]:
        """calendar.cancel is NOT supported for m365 — there is no restore path.

        The generic soft-delete flow maps calendar.cancel -> calendar_uncancel
        (its reversible promise), but Microsoft Graph cannot reinstate a
        cancelled event, so honouring that promise is impossible. Rather than
        perform an irreversible cancel behind a "reversible" gate, this returns
        a failure ActionResult (it never raises). The guardrail is still applied
        first so a clean (unapproved) environment is blocked exactly like every
        other mutation; when the gate would allow it, we still refuse with an
        explanation. Capability "calendar.cancel" is also False for m365
        (workspace_capabilities.py), so the generic execute path refuses these
        actions pre-execution via require_capability.
        """
        from workspace_guardrails import ActionResult, confirm_action

        if not confirm_action("calendar.cancel", event_id=event_id):
            return ActionResult(
                success=False, action="calendar.cancel", provider=self._provider_name,
                target=event_id, error="cancelled by guardrail",
            ).to_dict()
        return ActionResult(
            success=False, action="calendar.cancel", provider=self._provider_name,
            target=event_id,
            error="calendar.cancel is not supported for m365: Microsoft Graph has no "
                  "uncancel/restore path, so a cancel cannot be honoured behind the "
                  "reversible soft-delete promise. Cancel the event via Outlook, or "
                  "delete and recreate it.",
        ).to_dict()

    def calendar_uncancel(self, event_id: str) -> dict[str, Any]:
        raise NotImplementedError("Graph has no uncancel; recreate the event")

    # ── Files (OneDrive) ──────────────────────────────────────────────

    def files_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        try:
            escaped = str(query).replace("'", "''")
            path = f"{self._user_base()}/drive/root/search(q='{escaped}')"
            data = self._request("GET", path, params={"$top": max_results})
            value = data.get("value", []) if isinstance(data, Mapping) else []
            return [self._normalize_file(f) for f in value if isinstance(f, Mapping)]
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"m365 files_search failed: {exc}")
            return []

    @guarded("files.upload", target_arg="file_path", audit_provider="m365", audit_tool="graph_api")
    def files_upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        # Simple upload — supports files < 4 MB only. Larger files need an upload
        # session (deferred).
        p = Path(file_path)
        name = p.name
        content = p.read_bytes()
        if parent_id:
            path = f"{self._user_base()}/drive/items/{parent_id}:/{name}:/content"
        else:
            path = f"{self._user_base()}/drive/root:/{name}:/content"
        return self._request(
            "PUT", path, content=content,
            headers={"Content-Type": "application/octet-stream"},
        )

    @guarded("files.download", target_arg="file_id", audit_provider="m365", audit_tool="graph_api")
    def files_download(self, file_id: str, output_path: str) -> dict[str, Any]:
        content = self._request(
            "GET", f"{self._user_base()}/drive/items/{file_id}/content",
            raw=True, timeout=120,
        )
        if isinstance(content, (bytes, bytearray)):
            Path(output_path).write_bytes(bytes(content))
        return {"path": output_path}

    @guarded("files.trash", target_arg="file_id", audit_provider="m365", audit_tool="graph_api")
    def files_trash(self, file_id: str) -> dict[str, Any]:
        self._request("DELETE", f"{self._user_base()}/drive/items/{file_id}")
        return {"id": file_id, "reversible": True}  # goes to recycle bin

    # ── Health ────────────────────────────────────────────────────────

    def health_check(self) -> bool:
        try:
            self._request("GET", self._user_base(), params={"$select": "id"})
            return True
        except Exception:
            return False
