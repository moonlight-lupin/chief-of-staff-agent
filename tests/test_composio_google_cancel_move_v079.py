#!/usr/bin/env python3
"""v0.7.9 — Composio Google calendar.cancel (soft) + mail.move (label ids)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
for p in (SHARED_SCRIPTS, SHARED_SCRIPTS / "providers", PLUGIN_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _google_workspace(**extra):
    ws = {
        "provider": "composio",
        "mode": "mcp",
        "family": "google",
        "user_id": "test-user",
        "toolkits": ["gmail", "googlecalendar", "googledrive"],
        "mcp": {
            "endpoint": "https://connect.composio.dev/mcp",
            "key_env": "COMPOSIO_MCP_KEY",
        },
    }
    ws.update(extra)
    return {
        "integrations": {"workspace": ws},
        "paths": {"project_root": "/tmp/test-google-cancel-move"},
    }


def _ok(data):
    return {"data": {"results": [{"response": {"successful": True, "data": data}}]}}


@pytest.fixture
def mcp_key():
    os.environ["COMPOSIO_MCP_KEY"] = "test-key"
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
    yield
    os.environ.pop("COMPOSIO_MCP_KEY", None)
    os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)
    os.environ.pop("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", None)


@pytest.fixture
def tmp_project():
    with tempfile.TemporaryDirectory() as d:
        os.environ["CHIEF_OF_STAFF_PROJECT_ROOT"] = d
        yield Path(d)
        os.environ.pop("CHIEF_OF_STAFF_PROJECT_ROOT", None)


class TestGoogleCancelMoveSlugs:
    def test_new_google_slugs(self):
        from providers.composio_mcp_workspace_base import FAMILY_SLUGS
        g = FAMILY_SLUGS["google"]
        assert g["mail_move"] == "GMAIL_BATCH_MODIFY_MESSAGES"
        assert g["mail_modify_thread_labels"] == "GMAIL_MODIFY_THREAD_LABELS"
        assert g["calendar_delete"] == "GOOGLECALENDAR_DELETE_EVENT"
        assert g["calendar_batch"] == "GOOGLECALENDAR_BATCH_EVENTS"


class TestGoogleMailMove:
    def _client(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(_google_workspace())

    def test_move_to_label_id_batch_modify(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({})
        client._mcp_client = mock
        res = client.mail_move_to_folder("msg-hex", "Label_42")
        assert res["success"] is True
        assert res["action"] == "mail.move"
        assert res["data"]["destination"] == "Label_42"
        assert res["data"]["reversible"] is True
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "GMAIL_BATCH_MODIFY_MESSAGES"
        assert call["arguments"] == {
            "message_ids": ["msg-hex"],
            "add_label_ids": ["Label_42"],
            "remove_label_ids": ["INBOX"],
        }
        assert res["data"]["undo_add_label_ids"] == ["INBOX"]
        assert res["data"]["undo_remove_label_ids"] == ["Label_42"]

    def test_move_undo_round_trip_via_batch_modify(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({})
        client._mcp_client = mock
        moved = client.mail_move_to_folder("msg-hex", "Label_42")
        undo = moved["data"]
        client._google_batch_modify_labels(
            [undo["restore_target"]],
            add=undo["undo_add_label_ids"],
            remove=undo["undo_remove_label_ids"],
        )
        undo_call = mock.call_tool.call_args[0][1]["tools"][0]
        assert undo_call["arguments"] == {
            "message_ids": ["msg-hex"],
            "add_label_ids": ["INBOX"],
            "remove_label_ids": ["Label_42"],
        }

    def test_archive_remove_inbox_with_undo(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({})
        client._mcp_client = mock
        res = client.mail_move_to_folder("msg-1", "archive")
        assert res["success"] is True
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "GMAIL_ADD_LABEL_TO_EMAIL"
        assert call["arguments"]["remove_label_ids"] == ["INBOX"]
        assert res["data"]["undo_add_label_ids"] == ["INBOX"]
        assert res["data"]["undo_remove_label_ids"] == []

    def test_move_rejects_draft_id(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        client._mcp_client = mock
        res = client.mail_move_to_folder("r-999", "Label_1")
        assert res["success"] is False
        assert "draft id" in (res.get("error") or "").lower()
        assert mock.call_tool.call_count == 0

    def test_modify_thread_labels_helper(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({})
        client._mcp_client = mock
        client._google_modify_thread_labels(
            "thread-1", add=["Label_9"], remove=["INBOX"],
        )
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "GMAIL_MODIFY_THREAD_LABELS"
        assert call["arguments"] == {
            "thread_id": "thread-1",
            "add_label_ids": ["Label_9"],
            "remove_label_ids": ["INBOX"],
        }


class TestGoogleCalendarCancel:
    def _client(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(_google_workspace())

    def test_soft_cancel_uses_update_event(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "evt-1"})
        client._mcp_client = mock
        res = client.calendar_cancel("evt-1")
        assert res["success"] is True
        assert res["data"]["reversible"] is True
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "GOOGLECALENDAR_UPDATE_EVENT"
        assert call["arguments"] == {"event_id": "evt-1", "status": "cancelled"}

    def test_uncancel_uses_update_event(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({})
        client._mcp_client = mock
        res = client.calendar_uncancel("evt-1")
        assert res["success"] is True
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["arguments"]["status"] == "confirmed"

    def test_hard_delete_gated(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({})
        client._mcp_client = mock
        res = client.calendar_delete("evt-del")
        assert res["success"] is False
        os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = "1"
        res = client.calendar_delete("evt-del")
        assert res["success"] is True
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "GOOGLECALENDAR_DELETE_EVENT"
        assert call["arguments"] == {"event_id": "evt-del"}


class TestGoogleCancelMoveCapabilities:
    def test_caps_and_supports(self, mcp_key):
        from workspace_capabilities import UNSUPPORTED_REASONS, get_capabilities, supports
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        caps = get_capabilities("composio:mcp")
        assert caps["mail.move"] is True
        assert caps["calendar.cancel"] is True
        assert caps["calendar.uncancel"] is True
        assert caps["calendar.delete"] is True
        assert caps["mail.list_folders"] is True
        assert ("composio:mcp", "calendar.cancel") not in UNSUPPORTED_REASONS

        client = ComposioMCPWorkspaceClient(_google_workspace())
        assert client.supports("mail.move") is True
        assert client.supports("calendar.cancel") is True
        assert client.supports("calendar.uncancel") is True
        assert client.supports("calendar.delete") is True
        assert supports("composio", "calendar.cancel") is True
        assert supports("composio:mcp", "calendar.uncancel") is True


def _ms_workspace(**extra):
    ws = {
        "provider": "composio",
        "mode": "mcp",
        "family": "microsoft",
        "user_id": "test-user",
        "toolkits": ["outlook", "one_drive"],
        "mcp": {
            "endpoint": "https://connect.composio.dev/mcp",
            "key_env": "COMPOSIO_MCP_KEY",
        },
    }
    ws.update(extra)
    return {
        "integrations": {"workspace": ws},
        "paths": {"project_root": "/tmp/test-ms-cancel-refuse"},
    }


class TestMicrosoftCalendarCancelRefused:
    def _client(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(_ms_workspace())

    def test_calendar_cancel_not_implemented(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        client._mcp_client = mock
        res = client.calendar_cancel("evt-ms")
        assert res["success"] is False
        assert "not implemented" in res["error"].lower()
        assert mock.call_tool.call_count == 0

    def test_calendar_uncancel_not_implemented(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        client._mcp_client = mock
        res = client.calendar_uncancel("evt-ms")
        assert res["success"] is False
        assert "not implemented" in res["error"].lower()
        assert mock.call_tool.call_count == 0
