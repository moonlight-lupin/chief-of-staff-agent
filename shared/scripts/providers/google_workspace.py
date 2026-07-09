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


def _find_google_api_script() -> Path:
    """Locate google_api.py — check shared/scripts first, then installed skill."""
    candidates = [
        _PARENT / "google_api.py",
        Path.home() / ".hermes" / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py",
        Path.home() / ".hermes" / "skills" / "google-workspace" / "scripts" / "google_api.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        "google_api.py not found; install/configure google-workspace skill"
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

    def gmail_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        cmd = self._build_cmd("gmail", "search", query, "--max", str(max_results))
        rc, out, err = self._run(cmd)
        if rc != 0:
            warnings.warn(f"gmail_search failed: {err.strip() or out.strip()}")
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

    def drive_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        cmd = self._build_cmd("drive", "search", query, "--max", str(max_results))
        rc, out, err = self._run(cmd)
        if rc != 0:
            warnings.warn(f"drive_search failed: {err.strip() or out.strip()}")
            return []
        result = self._parse_json(out)
        return result if isinstance(result, list) else []

    # ── Write methods (guardrails + audit + ActionResult) ──────────────

    def drive_upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("drive.upload", file=file_path):
            return ActionResult(success=False, action="drive.upload", provider=self._provider_name,
                                target=file_path, error="cancelled by guardrail").to_dict()
        cmd = self._build_cmd("drive", "upload", file_path)
        if parent_id:
            cmd.extend(["--parent", parent_id])
        rc, out, err = self._run(cmd)
        if rc != 0:
            audit_workspace_action(self.config, "google_api", "drive.upload",
                                   "google_api.py", target=file_path, status="failed")
            return ActionResult(success=False, action="drive.upload", provider=self._provider_name,
                                target=file_path, error=err.strip() or out.strip(), audited=True).to_dict()
        try:
            data = json.loads(out) if out else {}
        except json.JSONDecodeError:
            data = {"raw": out.strip()}
        audit_workspace_action(self.config, "google_api", "drive.upload",
                               "google_api.py", target=file_path)
        return ActionResult(success=True, action="drive.upload", provider=self._provider_name,
                            target=file_path, data=data, audited=True).to_dict()

    def gmail_send(self, to: str, subject: str, body: str,
                    cc: str | None = None) -> dict[str, Any]:
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        # gmail.send is destructive — requires CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1
        if not confirm_action("gmail.send", to=to, subject=subject):
            return ActionResult(success=False, action="gmail.send", provider=self._provider_name,
                                target=to, error="cancelled by guardrail (requires CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1)").to_dict()
        cmd = self._build_cmd("gmail", "send", "--to", to, "--subject", subject, "--body", body)
        if cc:
            cmd.extend(["--cc", cc])
        rc, out, err = self._run(cmd)
        if rc != 0:
            audit_workspace_action(self.config, "google_api", "gmail.send",
                                   "google_api.py", target=to, status="failed")
            return ActionResult(success=False, action="gmail.send", provider=self._provider_name,
                                target=to, error=err.strip() or out.strip(), audited=True).to_dict()
        audit_workspace_action(self.config, "google_api", "gmail.send",
                               "google_api.py", target=to)
        return ActionResult(success=True, action="gmail.send", provider=self._provider_name,
                            target=to, data={"output": out.strip()}, audited=True).to_dict()

    def gmail_create_draft(self, to: str, subject: str, body: str,
                           cc: str | None = None) -> dict[str, Any]:
        """Create a Gmail draft.

        Note: google_api.py does not support a 'draft' subcommand.
        Gmail drafts are only supported through the Composio MCP provider.
        This method returns a clear 'not supported' error for the Google provider.
        """
        from workspace_guardrails import ActionResult
        return ActionResult(
            success=False, action="gmail.draft", provider=self._provider_name,
            target=to,
            error="gmail.draft not supported by google_api provider — use Composio MCP provider for draft creation",
            audited=False,
        ).to_dict()

    def calendar_create(self, title: str, start: str, end: str,
                        attendees: list[str] | None = None,
                        description: str | None = None) -> dict[str, Any]:
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("calendar.create", title=title):
            return ActionResult(success=False, action="calendar.create", provider=self._provider_name,
                                target=title, error="cancelled by guardrail").to_dict()
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
            audit_workspace_action(self.config, "google_api", "calendar.create",
                                   "google_api.py", target=title, status="failed")
            return ActionResult(success=False, action="calendar.create", provider=self._provider_name,
                                target=title, error=err.strip() or out.strip(), audited=True).to_dict()
        try:
            data = json.loads(out) if out else {}
        except json.JSONDecodeError:
            data = {"raw": out.strip()}
        audit_workspace_action(self.config, "google_api", "calendar.create",
                               "google_api.py", target=title)
        return ActionResult(success=True, action="calendar.create", provider=self._provider_name,
                            target=title, data=data, audited=True).to_dict()

    def calendar_update(self, event_id: str, **fields: Any) -> dict[str, Any]:
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("calendar.update", event_id=event_id):
            return ActionResult(success=False, action="calendar.update", provider=self._provider_name,
                                target=event_id, error="cancelled by guardrail").to_dict()
        cmd = self._build_cmd("calendar", "update", "--event-id", event_id)
        for key, value in fields.items():
            cmd.extend([f"--{key.replace('_', '-')}", str(value)])
        rc, out, err = self._run(cmd)
        if rc != 0:
            audit_workspace_action(self.config, "google_api", "calendar.update",
                                   "google_api.py", target=event_id, status="failed")
            return ActionResult(success=False, action="calendar.update", provider=self._provider_name,
                                target=event_id, error=err.strip() or out.strip(), audited=True).to_dict()
        try:
            data = json.loads(out) if out else {}
        except json.JSONDecodeError:
            data = {"raw": out.strip()}
        audit_workspace_action(self.config, "google_api", "calendar.update",
                               "google_api.py", target=event_id)
        return ActionResult(success=True, action="calendar.update", provider=self._provider_name,
                            target=event_id, data=data, audited=True).to_dict()

    def drive_download(self, file_id: str, output_path: str) -> dict[str, Any]:
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("drive.download", file_id=file_id):
            return ActionResult(success=False, action="drive.download", provider=self._provider_name,
                                target=file_id, error="cancelled by guardrail").to_dict()
        cmd = self._build_cmd("drive", "download", "--file-id", file_id, "--output", output_path)
        rc, out, err = self._run(cmd, timeout=120)
        if rc != 0:
            audit_workspace_action(self.config, "google_api", "drive.download",
                                   "google_api.py", target=file_id, status="failed")
            return ActionResult(success=False, action="drive.download", provider=self._provider_name,
                                target=file_id, error=err.strip() or out.strip(), audited=True).to_dict()
        audit_workspace_action(self.config, "google_api", "drive.download",
                               "google_api.py", target=file_id)
        return ActionResult(success=True, action="drive.download", provider=self._provider_name,
                            target=file_id, data={"path": output_path}, audited=True).to_dict()

    def gmail_archive(self, message_id: str) -> dict[str, Any]:
        """Archive a Gmail message (remove from INBOX). Reversible."""
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("gmail.archive", message_id=message_id):
            return ActionResult(success=False, action="gmail.archive", provider=self._provider_name,
                                target=message_id, error="cancelled by guardrail").to_dict()
        cmd = self._build_cmd("gmail", "modify", message_id, "--remove-labels", "INBOX")
        rc, out, err = self._run(cmd)
        if rc != 0:
            audit_workspace_action(self.config, "google_api", "gmail.archive",
                                   "google_api.py", target=message_id, status="failed")
            return ActionResult(success=False, action="gmail.archive", provider=self._provider_name,
                                target=message_id, error=err.strip() or out.strip(), audited=True).to_dict()
        audit_workspace_action(self.config, "google_api", "gmail.archive",
                               "google_api.py", target=message_id)
        return ActionResult(success=True, action="gmail.archive", provider=self._provider_name,
                            target=message_id, data={"output": out.strip()},
                            audited=True).to_dict()

    def gmail_trash(self, message_id: str) -> dict[str, Any]:
        """Move a Gmail message to trash. Reversible (30-day auto-delete by Google)."""
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("gmail.trash", message_id=message_id):
            return ActionResult(success=False, action="gmail.trash", provider=self._provider_name,
                                target=message_id, error="cancelled by guardrail").to_dict()
        cmd = self._build_cmd("gmail", "modify", message_id, "--add-labels", "TRASH", "--remove-labels", "INBOX")
        rc, out, err = self._run(cmd)
        if rc != 0:
            audit_workspace_action(self.config, "google_api", "gmail.trash",
                                   "google_api.py", target=message_id, status="failed")
            return ActionResult(success=False, action="gmail.trash", provider=self._provider_name,
                                target=message_id, error=err.strip() or out.strip(), audited=True).to_dict()
        audit_workspace_action(self.config, "google_api", "gmail.trash",
                               "google_api.py", target=message_id)
        return ActionResult(success=True, action="gmail.trash", provider=self._provider_name,
                            target=message_id, data={"output": out.strip(), "reversible": True},
                            audited=True).to_dict()

    def drive_trash(self, file_id: str) -> dict[str, Any]:
        """Move a Drive file to trash. Reversible (30-day auto-delete by Google)."""
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("drive.trash", file_id=file_id):
            return ActionResult(success=False, action="drive.trash", provider=self._provider_name,
                                target=file_id, error="cancelled by guardrail").to_dict()
        # drive delete defaults to trash (not --permanent)
        cmd = self._build_cmd("drive", "delete", file_id)
        rc, out, err = self._run(cmd)
        if rc != 0:
            audit_workspace_action(self.config, "google_api", "drive.trash",
                                   "google_api.py", target=file_id, status="failed")
            return ActionResult(success=False, action="drive.trash", provider=self._provider_name,
                                target=file_id, error=err.strip() or out.strip(), audited=True).to_dict()
        audit_workspace_action(self.config, "google_api", "drive.trash",
                               "google_api.py", target=file_id)
        return ActionResult(success=True, action="drive.trash", provider=self._provider_name,
                            target=file_id, data={"output": out.strip(), "reversible": True},
                            audited=True).to_dict()

    def calendar_cancel(self, event_id: str) -> dict[str, Any]:
        """Cancel a calendar event (set status to cancelled). Reversible via update."""
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("calendar.cancel", event_id=event_id):
            return ActionResult(success=False, action="calendar.cancel", provider=self._provider_name,
                                target=event_id, error="cancelled by guardrail").to_dict()
        cmd = self._build_cmd("calendar", "update", "--event-id", event_id, "--status", "cancelled")
        rc, out, err = self._run(cmd)
        if rc != 0:
            audit_workspace_action(self.config, "google_api", "calendar.cancel",
                                   "google_api.py", target=event_id, status="failed")
            return ActionResult(success=False, action="calendar.cancel", provider=self._provider_name,
                                target=event_id, error=err.strip() or out.strip(), audited=True).to_dict()
        audit_workspace_action(self.config, "google_api", "calendar.cancel",
                               "google_api.py", target=event_id)
        return ActionResult(success=True, action="calendar.cancel", provider=self._provider_name,
                            target=event_id, data={"output": out.strip(), "reversible": True},
                            audited=True).to_dict()

    def gmail_unarchive(self, message_id: str) -> dict[str, Any]:
        """Restore an archived Gmail message (add INBOX label back)."""
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("gmail.unarchive", message_id=message_id):
            return ActionResult(success=False, action="gmail.unarchive", provider=self._provider_name,
                                target=message_id, error="cancelled by guardrail").to_dict()
        cmd = self._build_cmd("gmail", "modify", message_id, "--add-labels", "INBOX")
        rc, out, err = self._run(cmd)
        if rc != 0:
            audit_workspace_action(self.config, "google_api", "gmail.unarchive",
                                   "google_api.py", target=message_id, status="failed")
            return ActionResult(success=False, action="gmail.unarchive", provider=self._provider_name,
                                target=message_id, error=err.strip() or out.strip(), audited=True).to_dict()
        audit_workspace_action(self.config, "google_api", "gmail.unarchive",
                               "google_api.py", target=message_id)
        return ActionResult(success=True, action="gmail.unarchive", provider=self._provider_name,
                            target=message_id, data={"output": out.strip()}, audited=True).to_dict()

    def gmail_untrash(self, message_id: str) -> dict[str, Any]:
        """Restore a trashed Gmail message (remove TRASH label)."""
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("gmail.untrash", message_id=message_id):
            return ActionResult(success=False, action="gmail.untrash", provider=self._provider_name,
                                target=message_id, error="cancelled by guardrail").to_dict()
        cmd = self._build_cmd("gmail", "modify", message_id, "--remove-labels", "TRASH")
        rc, out, err = self._run(cmd)
        if rc != 0:
            audit_workspace_action(self.config, "google_api", "gmail.untrash",
                                   "google_api.py", target=message_id, status="failed")
            return ActionResult(success=False, action="gmail.untrash", provider=self._provider_name,
                                target=message_id, error=err.strip() or out.strip(), audited=True).to_dict()
        audit_workspace_action(self.config, "google_api", "gmail.untrash",
                               "google_api.py", target=message_id)
        return ActionResult(success=True, action="gmail.untrash", provider=self._provider_name,
                            target=message_id, data={"output": out.strip()}, audited=True).to_dict()

    def calendar_uncancel(self, event_id: str) -> dict[str, Any]:
        """Restore a cancelled calendar event (set status back to confirmed)."""
        from workspace_audit import audit_workspace_action
        from workspace_guardrails import confirm_action, ActionResult
        if not confirm_action("calendar.uncancel", event_id=event_id):
            return ActionResult(success=False, action="calendar.uncancel", provider=self._provider_name,
                                target=event_id, error="cancelled by guardrail").to_dict()
        cmd = self._build_cmd("calendar", "update", "--event-id", event_id, "--status", "confirmed")
        rc, out, err = self._run(cmd)
        if rc != 0:
            audit_workspace_action(self.config, "google_api", "calendar.uncancel",
                                   "google_api.py", target=event_id, status="failed")
            return ActionResult(success=False, action="calendar.uncancel", provider=self._provider_name,
                                target=event_id, error=err.strip() or out.strip(), audited=True).to_dict()
        audit_workspace_action(self.config, "google_api", "calendar.uncancel",
                               "google_api.py", target=event_id)
        return ActionResult(success=True, action="calendar.uncancel", provider=self._provider_name,
                            target=event_id, data={"output": out.strip()}, audited=True).to_dict()

    def health_check(self) -> bool:
        cmd = self._build_cmd("calendar", "list")
        rc, _, _ = self._run(cmd, timeout=20)
        return rc == 0