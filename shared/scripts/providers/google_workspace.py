#!/usr/bin/env python3
"""Google Workspace backend for WorkspaceClient.

Wraps the existing google_api.py subprocess calls behind a clean interface.
Write methods use guardrails (confirm_action) and return ActionResult dicts.
All write actions are audited via workspace_audit.
"""
from __future__ import annotations

import base64
import functools
import json
import os
import subprocess
import sys
import uuid
import warnings
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Mapping

# Ensure parent dir is importable for workspace_client
_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from workspace_client import WorkspaceClient
from workspace_guardrails import guarded

# Draft creation uses gmail.modify (a superset of gmail.compose) because it is
# ALREADY in google_api.py's standard SCOPES / domain-wide delegation set — so
# drafts work with any existing google_api service-account setup, no new admin
# scope authorization required. gmail.compose is narrower but would force every
# operator to add a scope in Workspace Admin. Execution-verified 2026-07-16: the
# identical drafts.create call under gmail.modify returned HTTP 200 with the
# draft in the delegate's Drafts folder.
_GMAIL_DRAFT_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
_GMAIL_DRAFTS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
_GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
# Calendar create uses events.insert over REST so we can request a Meet link
# (google_api.py's calendar create CLI has no conferenceData support).
_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
_CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
# Drive untrash uses the same SA + delegate pattern as draft: google_api.py has
# no drive-untrash CLI, so we PATCH files.update with trashed=False over REST.
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"


def _sa_credentials(*, service_account_path: str, delegate_email: str, scopes: list[str]):
    """Build domain-wide-delegated SA credentials for a REST call."""
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    sa_path = Path(service_account_path).expanduser()
    if not sa_path.is_file():
        raise RuntimeError(f"service account JSON not found: {sa_path}")
    if not (delegate_email or "").strip():
        raise RuntimeError(
            "google.delegate_email is required for this Google REST operation"
        )
    credentials = service_account.Credentials.from_service_account_file(
        str(sa_path),
        scopes=scopes,
    ).with_subject(delegate_email.strip())
    credentials.refresh(Request())
    return credentials


def _gmail_draft_via_service_account(
    *,
    service_account_path: str,
    delegate_email: str,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
) -> dict[str, Any]:
    """Create a Gmail draft via REST using domain-wide delegation.

    ``google_api.py`` has no draft subcommand; this path uses the same SA +
    delegate identity already configured for the google_api provider.
    """
    import requests

    credentials = _sa_credentials(
        service_account_path=service_account_path,
        delegate_email=delegate_email,
        scopes=[_GMAIL_DRAFT_SCOPE],
    )

    mime = MIMEText(body or "", _charset="utf-8")
    mime["To"] = to
    mime["Subject"] = subject
    if cc:
        mime["Cc"] = cc
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")

    resp = requests.post(
        _GMAIL_DRAFTS_URL,
        headers={
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        },
        json={"message": {"raw": raw}},
        timeout=45,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Gmail drafts.create failed ({resp.status_code}): {resp.text[:500]}"
        )
    data = resp.json() if resp.content else {}
    draft_id = str(data.get("id") or "")
    msg = data.get("message") if isinstance(data.get("message"), Mapping) else {}
    msg_id = str(msg.get("id") or "") if isinstance(msg, Mapping) else ""
    out: dict[str, Any] = dict(data) if isinstance(data, dict) else {}
    if msg_id:
        out["draft_id"] = draft_id or out.get("id")
        out["id"] = msg_id
        out["message_id"] = msg_id
    elif draft_id:
        out["id"] = draft_id
        out["draft_id"] = draft_id
    return out


def _drive_untrash_via_service_account(
    *,
    service_account_path: str,
    delegate_email: str,
    file_id: str,
) -> dict[str, Any]:
    """Restore a trashed Drive file via REST (files.update trashed=False)."""
    import requests

    credentials = _sa_credentials(
        service_account_path=service_account_path,
        delegate_email=delegate_email,
        scopes=[_DRIVE_SCOPE],
    )
    resp = requests.patch(
        f"{_DRIVE_FILES_URL}/{file_id}",
        headers={
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        },
        params={"supportsAllDrives": "true", "fields": "id,name,trashed,webViewLink"},
        json={"trashed": False},
        timeout=45,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Drive files.update untrash failed ({resp.status_code}): {resp.text[:500]}"
        )
    data = resp.json() if resp.content else {}
    out: dict[str, Any] = dict(data) if isinstance(data, dict) else {}
    out.setdefault("id", file_id)
    out["reversible"] = True
    out["trashed"] = False
    return out


def _calendar_create_via_service_account(
    *,
    service_account_path: str,
    delegate_email: str,
    title: str,
    start: str,
    end: str,
    attendees: list[str] | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Insert a calendar event with a Meet link, then email attendees.

    ``google_api.py calendar create`` cannot request conferenceData (no Meet
    link) and service-account-created events do not reliably generate Google's
    own invitation email, so this path uses Calendar events.insert + a Gmail
    messages.send follow-up.
    """
    import requests

    credentials = _sa_credentials(
        service_account_path=service_account_path,
        delegate_email=delegate_email,
        scopes=[_CALENDAR_SCOPE],
    )

    attendee_emails = [str(a).strip() for a in (attendees or []) if str(a).strip()]
    body: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "conferenceData": {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    if attendee_emails:
        body["attendees"] = [{"email": a} for a in attendee_emails]
    if description:
        body["description"] = description

    resp = requests.post(
        _CALENDAR_EVENTS_URL,
        headers={
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        },
        params={"conferenceDataVersion": 1},
        json=body,
        timeout=45,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Calendar events.insert failed ({resp.status_code}): {resp.text[:500]}"
        )
    event = resp.json() if resp.content else {}
    if not isinstance(event, dict):
        event = {}

    hangout_link = event.get("hangoutLink") or ""
    out: dict[str, Any] = {
        "id": event.get("id"),
        "htmlLink": event.get("htmlLink"),
        "hangoutLink": event.get("hangoutLink"),
        "invite_message_id": None,
        "invite_sent_to": [],
    }

    if not attendee_emails:
        return out

    invite_error = _gmail_send_calendar_invite(
        service_account_path=service_account_path,
        delegate_email=delegate_email,
        title=title,
        start=start,
        end=end,
        attendees=attendee_emails,
        hangout_link=str(hangout_link) if hangout_link else "",
    )
    if invite_error.get("error"):
        out["error"] = invite_error["error"]
    else:
        out["invite_message_id"] = invite_error.get("invite_message_id")
        out["invite_sent_to"] = list(attendee_emails)
    return out


def _gmail_send_calendar_invite(
    *,
    service_account_path: str,
    delegate_email: str,
    title: str,
    start: str,
    end: str,
    attendees: list[str],
    hangout_link: str,
) -> dict[str, Any]:
    """Send one follow-up invite email covering every attendee.

    Never raises: insert already succeeded, so invite failure is partial
    success for the caller. Uses gmail.modify (same scope as drafts) rather
    than gmail.send so existing domain-wide delegation keeps working.
    """
    import requests

    meet_line = hangout_link or "(Meet link will appear on the calendar event)"
    body_text = (
        f"You are invited to: {title}\n"
        f"\n"
        f"When: {start} to {end}\n"
        f"\n"
        f"Join Google Meet: {meet_line}\n"
        f"\n"
        f"This event is also on the organizer's Google Calendar.\n"
    )
    # us-ascii / 7bit so the Gmail `raw` payload (urlsafe-b64 of the whole
    # MIME message) still contains the Meet link as plaintext after one decode —
    # utf-8 would wrap the body in a second base64 CTE and hide it.
    mime = MIMEText(body_text)
    mime["To"] = ", ".join(attendees)
    mime["Subject"] = f"Invitation: {title}"
    mime["From"] = delegate_email
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")

    try:
        credentials = _sa_credentials(
            service_account_path=service_account_path,
            delegate_email=delegate_email,
            scopes=[_GMAIL_DRAFT_SCOPE],
        )
        resp = requests.post(
            _GMAIL_SEND_URL,
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Content-Type": "application/json",
            },
            json={"message": {"raw": raw}},
            timeout=45,
        )
    except Exception as exc:  # noqa: BLE001 — invite failure must not drop the event
        return {"error": f"invite email failed: {exc}"}

    if resp.status_code >= 400:
        return {
            "error": (
                f"invite email failed ({resp.status_code}): {resp.text[:500]}"
            )
        }
    data = resp.json() if resp.content else {}
    msg_id = ""
    if isinstance(data, dict):
        msg_id = str(data.get("id") or "")
    return {"invite_message_id": msg_id}


def _calendar_create_contract(fn):
    """Adapt @guarded's ActionResult to the calendar.create contract.

    ``@guarded`` swallows body exceptions into ``success=False`` and always
    sets ``error=None`` on success. Contract tests require insert HTTP
    failures to raise ``RuntimeError``, and invite-send failures to keep
    ``success=True`` with a top-level ``error`` note.
    """

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        result = fn(self, *args, **kwargs)
        if not isinstance(result, dict):
            return result
        err = result.get("error") or ""
        if result.get("success") is False and "Calendar events.insert failed" in err:
            raise RuntimeError(err)
        data = result.get("data")
        if (
            result.get("success") is True
            and not result.get("error")
            and isinstance(data, dict)
            and data.get("error")
        ):
            result = dict(result)
            result["error"] = data["error"]
        return result

    return wrapper


def _find_google_api_script() -> Path:
    """Locate google_api.py — check shared/scripts first, then Hermes skill.

    Search order:
    1. shared/scripts/google_api.py (if shipped alongside this plugin)
    2. $CHIEF_OF_STAFF_HERMES_HOME/skills/productivity/google-workspace/scripts/
    3. $HERMES_HOME/skills/productivity/google-workspace/scripts/
    4. ~/.hermes/skills/productivity/google-workspace/scripts/ (default)

    The google-workspace skill is an OPTIONAL external dependency.
    Install it via: hermes skill install google-workspace
    Or set GOOGLE_WORKSPACE_API env var to point to a custom google_api.py.
    """
    # Check env override first
    env_script = os.getenv("GOOGLE_WORKSPACE_API")
    if env_script:
        p = Path(env_script).expanduser()
        if p.exists():
            return p

    # Determine Hermes home (env-configurable for non-Hermes agents)
    hermes_env = os.getenv("CHIEF_OF_STAFF_HERMES_HOME") or os.getenv("HERMES_HOME")
    hermes_home = Path(hermes_env).expanduser() if hermes_env else Path.home() / ".hermes"

    candidates = [
        _PARENT / "google_api.py",
        hermes_home / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py",
        hermes_home / "skills" / "google-workspace" / "scripts" / "google_api.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        "google_api.py not found. The google-workspace skill is an optional "
        "external dependency. Install it via 'hermes skill install google-workspace' "
        "or set GOOGLE_WORKSPACE_API to point to a custom google_api.py."
    )


class GoogleWorkspaceClient(WorkspaceClient):
    """Google Workspace provider using google_api.py subprocess."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self._provider_name = "google_api"
        google_cfg = config.get("google", {}) if isinstance(config, Mapping) else {}
        self.delegate_email = str(google_cfg.get("delegate_email", ""))
        self.account_alias = str(google_cfg.get("account_alias", ""))
        # If account_alias not set but service_account_path is, derive account name
        if not self.account_alias:
            sa_path = str(google_cfg.get("service_account_path", ""))
            if sa_path and "phronesis" in sa_path.lower():
                self.account_alias = "phronesis"
        self._script = _find_google_api_script()

    def _build_cmd(self, *args: str) -> list[str]:
        """Build a google_api.py command with auth flags."""
        cmd = [sys.executable, str(self._script)]
        if self.account_alias:
            cmd.extend(["--account", self.account_alias])
        if self.delegate_email:
            cmd.extend(["--as", self.delegate_email])
        cmd.extend(args)
        return cmd

    def _run(self, cmd: list[str], timeout: int = 45) -> tuple[int, str, str]:
        """Run a command, return (exit_code, stdout, stderr)."""
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "google_api.py timed out"
        except Exception as exc:
            return 1, "", str(exc)

    def _parse_json(self, stdout: str) -> list[dict[str, Any]] | str:
        """Parse JSON output, falling back to raw text."""
        try:
            result = json.loads(stdout or "[]")
            return result if isinstance(result, list) else [result]
        except (json.JSONDecodeError, TypeError):
            return stdout.strip() if stdout else []

    # ── Read methods (return lists, no guardrails needed) ──────────────

    def mail_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        cmd = self._build_cmd("gmail", "search", query, "--max", str(max_results))
        rc, out, err = self._run(cmd)
        if rc != 0:
            warnings.warn(f"mail_search failed: {err.strip() or out.strip()}")
            return []
        result = self._parse_json(out)
        return result if isinstance(result, list) else []

    def calendar_list(self, start: str, end: str) -> list[dict[str, Any]]:
        # Ensure RFC3339 format (google_api.py requires datetime, not bare date)
        if "T" not in start:
            start = f"{start}T00:00:00Z"
        if "T" not in end:
            end = f"{end}T23:59:59Z"
        cmd = self._build_cmd("calendar", "list", "--start", start, "--end", end)
        rc, out, err = self._run(cmd)
        if rc != 0:
            warnings.warn(f"calendar_list failed: {err.strip() or out.strip()}")
            return []
        result = self._parse_json(out)
        return result if isinstance(result, list) else []

    def files_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        cmd = self._build_cmd("drive", "search", query, "--max", str(max_results))
        rc, out, err = self._run(cmd)
        if rc != 0:
            warnings.warn(f"files_search failed: {err.strip() or out.strip()}")
            return []
        result = self._parse_json(out)
        return result if isinstance(result, list) else []

    def mail_list_tags(self) -> list[dict[str, Any]]:
        """List all Gmail labels (tags). Read-only — no mutation."""
        cmd = self._build_cmd("gmail", "labels")
        rc, out, err = self._run(cmd, timeout=30)
        if rc != 0:
            warnings.warn(f"mail_list_tags failed: {err.strip() or out.strip()}")
            return []
        try:
            labels = json.loads(out) if out else []
        except json.JSONDecodeError:
            return []
        return labels if isinstance(labels, list) else []

    # ── Write methods (guarded: confirm + audit + ActionResult) ────────
    # Each body performs the raw work and returns a data dict, or raises on
    # failure; @guarded wraps it. Legacy action ids ("gmail.send", ...) are
    # preserved for back-compat with stored queues and tests.

    @guarded("drive.upload", target_arg="file_path", audit_provider="google_api")
    def files_upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        cmd = self._build_cmd("drive", "upload", file_path)
        if parent_id:
            cmd.extend(["--parent", parent_id])
        rc, out, err = self._run(cmd)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        try:
            return json.loads(out) if out else {}
        except json.JSONDecodeError:
            return {"raw": out.strip()}

    @guarded("gmail.send", target_arg="to", audit_provider="google_api",
             block_error="cancelled by guardrail (requires CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1)")
    def mail_send(self, to: str, subject: str, body: str,
                  cc: str | None = None) -> dict[str, Any]:
        # gmail.send is destructive — requires CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1
        cmd = self._build_cmd("gmail", "send", "--to", to, "--subject", subject, "--body", body)
        if cc:
            cmd.extend(["--cc", cc])
        rc, out, err = self._run(cmd)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        return {"output": out.strip()}

    @guarded("gmail.draft", target_arg="to", audit_provider="google_api")
    def mail_create_draft(self, to: str, subject: str, body: str,
                          cc: str | None = None) -> dict[str, Any]:
        """Create a mail draft.

        ``google_api.py`` has no draft CLI; drafts are created via the Gmail
        REST API using the configured service-account + ``delegate_email``.
        Surfaces the underlying message id as ``id`` (keeps ``draft_id``),
        matching the Composio Google contract.
        """
        google_cfg = (
            self.config.get("google", {})
            if isinstance(self.config, Mapping)
            else {}
        )
        sa_path = str(google_cfg.get("service_account_path") or "").strip()
        if not sa_path:
            sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH", "").strip()
        delegate = self.delegate_email or str(
            google_cfg.get("delegate_email") or ""
        ).strip()
        if not sa_path:
            raise RuntimeError(
                "gmail.draft requires google.service_account_path (or "
                "GOOGLE_SERVICE_ACCOUNT_PATH) — google_api.py has no draft CLI"
            )
        return _gmail_draft_via_service_account(
            service_account_path=sa_path,
            delegate_email=delegate,
            to=to,
            subject=subject,
            body=body,
            cc=cc,
        )

    @_calendar_create_contract
    @guarded("calendar.create", target_arg="title", audit_provider="google_api")
    def calendar_create(self, title: str, start: str, end: str,
                        attendees: list[str] | None = None,
                        description: str | None = None) -> dict[str, Any]:
        """Create a calendar event with a Google Meet link (Meet-by-default).

        Inserts via Calendar REST (conferenceDataVersion=1) using the
        configured service account + delegate, then sends one follow-up
        Gmail invite to attendees. Service-account-created events do not
        reliably generate Google's own invitation email.
        """
        # Date-only strings still get a default time so callers that passed
        # YYYY-MM-DD to the old CLI keep working; RFC3339 values are used as-is.
        if "T" not in start:
            start = f"{start}T10:00:00Z"
        if "T" not in end:
            end = f"{end}T11:00:00Z"
        google_cfg = (
            self.config.get("google", {})
            if isinstance(self.config, Mapping)
            else {}
        )
        sa_path = str(google_cfg.get("service_account_path") or "").strip()
        if not sa_path:
            sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH", "").strip()
        delegate = self.delegate_email or str(
            google_cfg.get("delegate_email") or ""
        ).strip()
        if not sa_path:
            raise RuntimeError(
                "calendar.create requires google.service_account_path (or "
                "GOOGLE_SERVICE_ACCOUNT_PATH) — events.insert is REST-only"
            )
        return _calendar_create_via_service_account(
            service_account_path=sa_path,
            delegate_email=delegate,
            title=title,
            start=start,
            end=end,
            attendees=attendees,
            description=description,
        )

    @guarded("calendar.update", target_arg="event_id", audit_provider="google_api")
    def calendar_update(self, event_id: str, **fields: Any) -> dict[str, Any]:
        cmd = self._build_cmd("calendar", "update", "--event-id", event_id)
        for key, value in fields.items():
            cmd.extend([f"--{key.replace('_', '-')}", str(value)])
        rc, out, err = self._run(cmd)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        try:
            return json.loads(out) if out else {}
        except json.JSONDecodeError:
            return {"raw": out.strip()}

    @guarded("drive.download", target_arg="file_id", audit_provider="google_api")
    def files_download(self, file_id: str, output_path: str) -> dict[str, Any]:
        cmd = self._build_cmd("drive", "download", "--file-id", file_id, "--output", output_path)
        rc, out, err = self._run(cmd, timeout=120)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        return {"path": output_path}

    @guarded("gmail.archive", target_arg="message_id", audit_provider="google_api")
    def mail_archive(self, message_id: str) -> dict[str, Any]:
        """Archive a mail message (remove from INBOX). Reversible."""
        cmd = self._build_cmd("gmail", "modify", message_id, "--remove-labels", "INBOX")
        rc, out, err = self._run(cmd)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        return {"output": out.strip()}

    @guarded("gmail.trash", target_arg="message_id", audit_provider="google_api")
    def mail_trash(self, message_id: str) -> dict[str, Any]:
        """Move a mail message to trash. Reversible (30-day auto-delete by Google)."""
        cmd = self._build_cmd("gmail", "modify", message_id, "--add-labels", "TRASH", "--remove-labels", "INBOX")
        rc, out, err = self._run(cmd)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        return {"output": out.strip(), "reversible": True}

    @guarded("drive.trash", target_arg="file_id", audit_provider="google_api")
    def files_trash(self, file_id: str) -> dict[str, Any]:
        """Move a Drive file to trash. Reversible (30-day auto-delete by Google)."""
        # drive delete defaults to trash (not --permanent)
        cmd = self._build_cmd("drive", "delete", file_id)
        rc, out, err = self._run(cmd)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        return {"output": out.strip(), "reversible": True}

    @guarded("drive.untrash", target_arg="file_id", audit_provider="google_api")
    def files_untrash(self, file_id: str) -> dict[str, Any]:
        """Restore a trashed Drive file (files.update trashed=False via SA REST).

        ``google_api.py`` has no drive-untrash CLI; this mirrors the
        ``mail_create_draft`` SA REST path.
        """
        google_cfg = (
            self.config.get("google", {})
            if isinstance(self.config, Mapping)
            else {}
        )
        sa_path = str(google_cfg.get("service_account_path") or "").strip()
        if not sa_path:
            sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH", "").strip()
        delegate = self.delegate_email or str(
            google_cfg.get("delegate_email") or ""
        ).strip()
        if not sa_path:
            raise RuntimeError(
                "files.untrash requires google.service_account_path (or "
                "GOOGLE_SERVICE_ACCOUNT_PATH) — google_api.py has no untrash CLI"
            )
        return _drive_untrash_via_service_account(
            service_account_path=sa_path,
            delegate_email=delegate,
            file_id=file_id,
        )

    @guarded("calendar.cancel", target_arg="event_id", audit_provider="google_api")
    def calendar_cancel(self, event_id: str) -> dict[str, Any]:
        """Cancel a calendar event (set status to cancelled). Reversible via update."""
        cmd = self._build_cmd("calendar", "update", "--event-id", event_id, "--status", "cancelled")
        rc, out, err = self._run(cmd)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        return {"output": out.strip(), "reversible": True}

    @guarded("gmail.label", target_arg="message_id", audit_provider="google_api")
    def mail_tag(self, message_id: str, tag_id: str) -> dict[str, Any]:
        """Apply an existing tag (Gmail label) to a message. Guardrailed + audited."""
        cmd = self._build_cmd("gmail", "modify", message_id, "--add-labels", tag_id)
        rc, out, err = self._run(cmd)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        return {"label_id": tag_id}

    @guarded("gmail.create_label", target_arg="name", audit_provider="google_api")
    def mail_create_tag(self, name: str) -> dict[str, Any]:
        """Create a new tag (Gmail label). Guardrailed + audited.
        Note: google_api.py may not support label creation yet.
        """
        # google_api.py may not have a labels --create subcommand.
        cmd = self._build_cmd("gmail", "labels", "--create", name)
        rc, out, err = self._run(cmd)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip() or "label creation not supported by google_api.py")
        try:
            return json.loads(out) if out else {}
        except json.JSONDecodeError:
            return {"raw": out.strip()}

    @guarded("gmail.unarchive", target_arg="message_id", audit_provider="google_api")
    def mail_unarchive(self, message_id: str) -> dict[str, Any]:
        """Restore an archived mail message (add INBOX label back)."""
        cmd = self._build_cmd("gmail", "modify", message_id, "--add-labels", "INBOX")
        rc, out, err = self._run(cmd)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        return {"output": out.strip()}

    @guarded("gmail.untrash", target_arg="message_id", audit_provider="google_api")
    def mail_untrash(self, message_id: str) -> dict[str, Any]:
        """Restore a trashed mail message (remove TRASH label)."""
        cmd = self._build_cmd("gmail", "modify", message_id, "--remove-labels", "TRASH")
        rc, out, err = self._run(cmd)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        return {"output": out.strip()}

    @guarded("calendar.uncancel", target_arg="event_id", audit_provider="google_api")
    def calendar_uncancel(self, event_id: str) -> dict[str, Any]:
        """Restore a cancelled calendar event (set status back to confirmed)."""
        cmd = self._build_cmd("calendar", "update", "--event-id", event_id, "--status", "confirmed")
        rc, out, err = self._run(cmd)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        return {"output": out.strip()}

    def health_check(self) -> bool:
        cmd = self._build_cmd("calendar", "list")
        rc, _, _ = self._run(cmd, timeout=20)
        return rc == 0