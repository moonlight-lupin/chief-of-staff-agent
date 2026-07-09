#!/usr/bin/env python3
"""Google Workspace backend for WorkspaceClient.

Wraps the existing google_api.py subprocess calls behind a clean interface.
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

    def gmail_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        cmd = self._build_cmd("gmail", "search", query, "--max", str(max_results))
        rc, out, err = self._run(cmd)
        if rc != 0:
            warnings.warn(f"gmail_search failed: {err.strip() or out.strip()}")
            return []
        result = self._parse_json(out)
        return result if isinstance(result, list) else []

    def calendar_list(self, start: str, end: str) -> list[dict[str, Any]]:
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

    def drive_upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        cmd = self._build_cmd("drive", "upload", "--file", file_path)
        if parent_id:
            cmd.extend(["--parent", parent_id])
        rc, out, err = self._run(cmd)
        if rc != 0:
            return {"error": err.strip() or out.strip(), "success": False}
        try:
            return json.loads(out) if out else {"success": True}
        except json.JSONDecodeError:
            return {"success": True, "raw": out.strip()}

    def gmail_send(self, to: str, subject: str, body: str) -> dict[str, Any]:
        cmd = self._build_cmd("gmail", "send", "--to", to, "--subject", subject, "--body", body)
        rc, out, err = self._run(cmd)
        if rc != 0:
            return {"error": err.strip() or out.strip(), "success": False}
        return {"success": True, "output": out.strip()}

    def gmail_create_draft(self, to: str, subject: str, body: str,
                           cc: str | None = None) -> dict[str, Any]:
        cmd = self._build_cmd("gmail", "draft", "--to", to, "--subject", subject, "--body", body)
        if cc:
            cmd.extend(["--cc", cc])
        rc, out, err = self._run(cmd)
        if rc != 0:
            return {"error": err.strip() or out.strip(), "success": False}
        try:
            return json.loads(out) if out else {"success": True}
        except json.JSONDecodeError:
            return {"success": True, "raw": out.strip()}

    def calendar_create(self, title: str, start: str, end: str,
                        attendees: list[str] | None = None,
                        description: str | None = None) -> dict[str, Any]:
        cmd = self._build_cmd("calendar", "create", "--title", title,
                              "--start", start, "--end", end)
        if attendees:
            cmd.extend(["--attendees", ",".join(attendees)])
        if description:
            cmd.extend(["--description", description])
        rc, out, err = self._run(cmd)
        if rc != 0:
            return {"error": err.strip() or out.strip(), "success": False}
        try:
            return json.loads(out) if out else {"success": True}
        except json.JSONDecodeError:
            return {"success": True, "raw": out.strip()}

    def calendar_update(self, event_id: str, **fields: Any) -> dict[str, Any]:
        cmd = self._build_cmd("calendar", "update", "--event-id", event_id)
        for key, value in fields.items():
            cmd.extend([f"--{key.replace('_', '-')}", str(value)])
        rc, out, err = self._run(cmd)
        if rc != 0:
            return {"error": err.strip() or out.strip(), "success": False}
        try:
            return json.loads(out) if out else {"success": True}
        except json.JSONDecodeError:
            return {"success": True, "raw": out.strip()}

    def drive_download(self, file_id: str, output_path: str) -> dict[str, Any]:
        cmd = self._build_cmd("drive", "download", "--file-id", file_id, "--output", output_path)
        rc, out, err = self._run(cmd, timeout=120)
        if rc != 0:
            return {"error": err.strip() or out.strip(), "success": False}
        return {"success": True, "path": output_path}

    def health_check(self) -> bool:
        cmd = self._build_cmd("calendar", "list")
        rc, _, _ = self._run(cmd, timeout=20)
        return rc == 0