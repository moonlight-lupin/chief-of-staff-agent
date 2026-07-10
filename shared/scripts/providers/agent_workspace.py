#!/usr/bin/env python3
"""Agent-native workspace backend for WorkspaceClient.

This provider is a deliberate no-op fetcher. Under the "fetch/compute split",
workspace data (mail, calendar, files) is fetched by the AI agent (Claude) using
its OWN native connector tools (Google Workspace or Microsoft 365) — NOT by any
Python API client. The agent then writes a JSON envelope conforming to the
workspace payload schema in ``shared/scripts/schemas.py`` and passes it to the
compute scripts (daily_briefing.py, weekly-review workspace_collect.py,
meeting-prep workspace_actions.py) via ``--input PATH``.

Because there is no Python client to call, every mail_*/calendar_*/files_*
method here raises NotImplementedError with an actionable message. ``health_check``
returns True (the "provider" is always available — it is just the agent itself).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Ensure parent dir is importable for workspace_client
_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from workspace_client import WorkspaceClient

_GUIDANCE = (
    "provider 'agent': workspace data is fetched by the AI agent's native "
    "connector tools (Google Workspace or Microsoft 365), not by Python. "
    "Fetch the data with your tools, write a JSON envelope matching "
    "shared/scripts/schemas.py workspace payload schema, and pass it via "
    "--input to the aggregate script."
)


class AgentWorkspaceClient(WorkspaceClient):
    """No-op workspace provider for the agent-native fetch/compute split.

    All read/write methods raise NotImplementedError with guidance pointing the
    agent at the ``--input`` JSON-envelope workflow.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self._provider_name = "agent"

    def _unsupported(self, method: str) -> "NotImplementedError":
        return NotImplementedError(f"{method}: {_GUIDANCE}")

    # ── Mail ───────────────────────────────────────────────────────────
    def mail_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        raise self._unsupported("mail_search")

    def mail_create_draft(self, to: str, subject: str, body: str,
                          cc: str | None = None) -> dict[str, Any]:
        raise self._unsupported("mail_create_draft")

    def mail_send(self, to: str, subject: str, body: str,
                  cc: str | None = None) -> dict[str, Any]:
        raise self._unsupported("mail_send")

    def mail_archive(self, message_id: str) -> dict[str, Any]:
        raise self._unsupported("mail_archive")

    def mail_unarchive(self, message_id: str) -> dict[str, Any]:
        raise self._unsupported("mail_unarchive")

    def mail_trash(self, message_id: str) -> dict[str, Any]:
        raise self._unsupported("mail_trash")

    def mail_untrash(self, message_id: str) -> dict[str, Any]:
        raise self._unsupported("mail_untrash")

    def mail_list_tags(self) -> list[dict[str, Any]]:
        raise self._unsupported("mail_list_tags")

    def mail_tag(self, message_id: str, tag_id: str) -> dict[str, Any]:
        raise self._unsupported("mail_tag")

    def mail_create_tag(self, name: str) -> dict[str, Any]:
        raise self._unsupported("mail_create_tag")

    # ── Calendar ───────────────────────────────────────────────────────
    def calendar_list(self, start: str, end: str) -> list[dict[str, Any]]:
        raise self._unsupported("calendar_list")

    def calendar_create(self, title: str, start: str, end: str,
                        attendees: list[str] | None = None,
                        description: str | None = None) -> dict[str, Any]:
        raise self._unsupported("calendar_create")

    def calendar_update(self, event_id: str, **fields: Any) -> dict[str, Any]:
        raise self._unsupported("calendar_update")

    def calendar_cancel(self, event_id: str) -> dict[str, Any]:
        raise self._unsupported("calendar_cancel")

    # ── Files ──────────────────────────────────────────────────────────
    def files_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        raise self._unsupported("files_search")

    def files_upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        raise self._unsupported("files_upload")

    def files_download(self, file_id: str, output_path: str) -> dict[str, Any]:
        raise self._unsupported("files_download")

    def files_trash(self, file_id: str) -> dict[str, Any]:
        raise self._unsupported("files_trash")

    # ── Health ─────────────────────────────────────────────────────────
    def health_check(self) -> bool:
        """The agent provider is always available (it is the agent itself)."""
        return True
