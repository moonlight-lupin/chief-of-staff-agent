#!/usr/bin/env python3
"""Wrapper over google-workspace skill's google_api.py.

Usage:
    from google_client import GmailClient, CalendarClient, DriveClient
    gmail = GmailClient(config)
    results = gmail.search("is:unread", max_results=10)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


class GoogleClientError(RuntimeError):
    """Raised for missing tooling, auth failures, API errors, or parse failures."""


class GoogleClient:
    """Base wrapper around ``google_api.py`` with JSON parsing and config env."""

    def __init__(self, config: Mapping[str, Any] | None = None, api_script: str | os.PathLike[str] | None = None) -> None:
        self.config = config or {}
        self.google_config = self.config.get("google", {}) if isinstance(self.config.get("google", {}), Mapping) else {}
        self.api_script = Path(api_script).expanduser() if api_script else self._find_google_api()
        if not self.api_script.exists():
            raise GoogleClientError(
                f"google_api.py not found at {self.api_script}. Install the google-workspace skill or pass api_script=."
            )

    def _find_google_api(self) -> Path:
        candidates = []
        hermes_home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
        candidates.append(hermes_home / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py")
        candidates.append(hermes_home / "skills" / "google-workspace" / "scripts" / "google_api.py")
        for path in candidates:
            if path.exists():
                return path
        return candidates[0]

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        mapping = {
            "service_account_path": "GOOGLE_SERVICE_ACCOUNT_PATH",
            "domain": "GOOGLE_WORKSPACE_DOMAIN",
            "delegate_email": "GOOGLE_DELEGATE_EMAIL",
        }
        for key, env_key in mapping.items():
            value = self.google_config.get(key)
            if value:
                env[env_key] = str(Path(value).expanduser() if key.endswith("path") else value)
        return env

    def _run(self, *args: str, input_json: Mapping[str, Any] | None = None) -> Any:
        cmd = [sys.executable, str(self.api_script), *map(str, args)]
        if input_json is not None:
            cmd.extend(["--json", json.dumps(input_json, default=str)])
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=120, env=self._env(), check=False)
        except FileNotFoundError as exc:
            raise GoogleClientError(f"Python or google_api.py not found while running {cmd!r}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GoogleClientError(f"Google API command timed out: {' '.join(cmd)}") from exc
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            combined = "\n".join(part for part in (stdout, stderr) if part)
            if "NOT_AUTHENTICATED" in combined or "auth" in combined.lower() or "credential" in combined.lower():
                raise GoogleClientError(f"Google auth failure: {combined or 'command returned non-zero'}")
            raise GoogleClientError(f"Google API error ({proc.returncode}): {combined or 'no output'}")
        if not stdout:
            return []
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise GoogleClientError(f"Google API returned non-JSON output: {stdout[:500]}") from exc


class GmailClient(GoogleClient):
    def search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        result = self._run("gmail", "search", query, "--max", str(max_results))
        return result if isinstance(result, list) else []

    def get_message(self, id: str) -> dict[str, Any]:
        result = self._run("gmail", "get", id)
        return result if isinstance(result, dict) else {"result": result}

    def mark_read(self, id: str) -> dict[str, Any]:
        result = self._run("gmail", "modify", id, "--remove-labels", "UNREAD")
        return result if isinstance(result, dict) else {"result": result}

    def list_attachments(self, msg_id: str) -> list[dict[str, Any]]:
        """List all attachments in a Gmail message."""
        result = self._run("gmail", "attachments", msg_id)
        return result if isinstance(result, list) else []

    def download_attachment(
        self,
        msg_id: str,
        *,
        filename: str = "",
        attachment_id: str = "",
        output_dir: str = "/tmp",
        output_name: str = "",
    ) -> dict[str, Any]:
        """Download a Gmail attachment by filename or attachment ID."""
        args = ["gmail", "attachment-download", msg_id]
        if filename:
            args.extend(["--filename", filename])
        elif attachment_id:
            args.extend(["--attachment-id", attachment_id])
        if output_dir:
            args.extend(["--output-dir", output_dir])
        if output_name:
            args.extend(["--output-name", output_name])
        result = self._run(*args)
        return result if isinstance(result, dict) else {"result": result}

    def get_attachment(self, msg_id: str, attachment_id: str) -> dict[str, Any]:
        """Deprecated: use download_attachment instead. Kept for backward compat."""
        return self.download_attachment(msg_id, attachment_id=attachment_id)


class CalendarClient(GoogleClient):
    def list(self, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:  # noqa: A003 - public API requested
        args = ["calendar", "list"]
        if start:
            args.extend(["--start", start])
        if end:
            args.extend(["--end", end])
        result = self._run(*args)
        return result if isinstance(result, list) else []

    def create(self, event_data: Mapping[str, Any]) -> dict[str, Any]:
        args = ["calendar", "create"]
        for key, flag in (("summary", "--summary"), ("start", "--start"), ("end", "--end"), ("location", "--location"), ("description", "--description"), ("attendees", "--attendees")):
            value = event_data.get(key)
            if value:
                if isinstance(value, list):
                    value = ",".join(map(str, value))
                args.extend([flag, str(value)])
        result = self._run(*args)
        return result if isinstance(result, dict) else {"result": result}

    def update(self, event_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        result = self._run("calendar", "update", event_id, input_json=data)
        return result if isinstance(result, dict) else {"result": result}

    def delete(self, event_id: str) -> dict[str, Any]:
        result = self._run("calendar", "delete", event_id)
        return result if isinstance(result, dict) else {"result": result}


class DriveClient(GoogleClient):
    def search(self, query: str, max: int = 10) -> list[dict[str, Any]]:  # noqa: A002 - public API requested
        result = self._run("drive", "search", query, "--max", str(max))
        return result if isinstance(result, list) else []

    def upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        args = ["drive", "upload", file_path]
        if parent_id:
            args.extend(["--parent", parent_id])
        result = self._run(*args)
        return result if isinstance(result, dict) else {"result": result}

    def download(self, file_id: str, output: str | None = None) -> dict[str, Any]:
        args = ["drive", "download", file_id]
        if output:
            args.extend(["--output", output])
        result = self._run(*args)
        return result if isinstance(result, dict) else {"result": result}

    def create_folder(self, name: str, parent_id: str | None = None) -> dict[str, Any]:
        args = ["drive", "create-folder", name]
        if parent_id:
            args.extend(["--parent", parent_id])
        result = self._run(*args)
        return result if isinstance(result, dict) else {"result": result}

    def share(self, file_id: str, email: str, role: str) -> dict[str, Any]:
        result = self._run("drive", "share", file_id, "--email", email, "--role", role)
        return result if isinstance(result, dict) else {"result": result}


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Thin JSON wrapper around google-workspace google_api.py")
    parser.add_argument("service", choices=["gmail", "calendar", "drive"], help="Google service to call")
    parser.add_argument("command", help="Command/method, e.g. search, list, get")
    parser.add_argument("args", nargs="*", help="Arguments forwarded to google_api.py")
    parser.add_argument("--api-script", help="Explicit path to google_api.py")
    parser.add_argument("--json", action="store_true", help="Pretty-print parsed JSON output")
    ns = parser.parse_args(argv)
    client = GoogleClient(api_script=ns.api_script)
    result = client._run(ns.service, ns.command, *ns.args)
    if ns.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
