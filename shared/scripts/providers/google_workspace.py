#!/usr/bin/env python3
"""Google Workspace backend for WorkspaceClient.

Wraps the existing google_api.py subprocess calls behind a clean interface.
Write methods use guardrails (confirm_action) and return ActionResult dicts.
All write actions are audited via workspace_audit.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any, Mapping

# Ensure parent dir is importable for workspace_client
_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from workspace_client import WorkspaceClient
from workspace_guardrails import guarded


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

    def mail_create_draft(self, to: str, subject: str, body: str,
                          cc: str | None = None) -> dict[str, Any]:
        """Create a mail draft.

        Note: google_api.py does not support a 'draft' subcommand.
        Drafts are only supported through the Composio MCP provider.
        This method returns a clear 'not supported' error for the Google provider.
        """
        from workspace_guardrails import ActionResult
        return ActionResult(
            success=False, action="gmail.draft", provider=self._provider_name,
            target=to,
            error="gmail.draft not supported by google_api provider — use Composio MCP provider for draft creation",
            audited=False,
        ).to_dict()

    @guarded("calendar.create", target_arg="title", audit_provider="google_api")
    def calendar_create(self, title: str, start: str, end: str,
                        attendees: list[str] | None = None,
                        description: str | None = None) -> dict[str, Any]:
        # Ensure RFC3339 format (google_api.py requires ISO 8601 with timezone)
        if "T" not in start:
            start = f"{start}T10:00:00Z"
        if "T" not in end:
            end = f"{end}T11:00:00Z"
        cmd = self._build_cmd("calendar", "create", "--summary", title,
                              "--start", start, "--end", end)
        if attendees:
            cmd.extend(["--attendees", ",".join(attendees)])
        if description:
            cmd.extend(["--description", description])
        rc, out, err = self._run(cmd)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        try:
            return json.loads(out) if out else {}
        except json.JSONDecodeError:
            return {"raw": out.strip()}

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