#!/usr/bin/env python3
"""v0.3.11 — Composio Microsoft Phase 3 folders + approval-gated mail.send."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
for p in (SHARED_SCRIPTS, SHARED_SCRIPTS / "providers", PLUGIN_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


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
        "paths": {"project_root": "/tmp/test-ms-phase3"},
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


class TestFamilySlugsPhase3:
    def test_microsoft_send_and_folder_slugs(self):
        from providers.composio_mcp_workspace_base import FAMILY_SLUGS
        ms = FAMILY_SLUGS["microsoft"]
        assert ms["mail_send"] == "OUTLOOK_SEND_EMAIL"
        assert ms["mail_list_folders"] == "OUTLOOK_LIST_MAIL_FOLDERS"
        assert ms["mail_move"] == "OUTLOOK_MOVE_MESSAGE"
        assert "mail_send" not in FAMILY_SLUGS["google"]
        assert "mail_list_folders" not in FAMILY_SLUGS["google"]


class TestMailFoldersAndMove:
    def _client(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(_ms_workspace())

    def test_list_folders_normalizes_graph_shape(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({
            "value": [
                {
                    "id": "AAMkAGI0",
                    "displayName": "Billing",
                    "parentFolderId": "root",
                    "unreadItemCount": 2,
                    "totalItemCount": 9,
                    "childFolderCount": 0,
                    "isHidden": False,
                }
            ]
        })
        client._mcp_client = mock

        folders = client.mail_list_folders(max_results=10)
        assert folders == [{
            "id": "AAMkAGI0",
            "name": "Billing",
            "parent_id": "root",
            "unread": 2,
            "total": 9,
            "child_count": 0,
            "hidden": False,
        }]
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "OUTLOOK_LIST_MAIL_FOLDERS"
        assert call["arguments"]["top"] == 10

    def test_move_to_folder_uses_destination_id(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "moved-1"})
        client._mcp_client = mock

        res = client.mail_move_to_folder("msg-1", "AAMkAGI0")
        assert res["success"] is True
        assert res["action"] == "mail.move"
        assert res["data"]["destination"] == "AAMkAGI0"
        assert res["data"]["restore_target"] == "moved-1"
        args = mock.call_tool.call_args[0][1]["tools"][0]["arguments"]
        assert args == {"message_id": "msg-1", "destination_id": "AAMkAGI0"}

    def test_resolve_folder_by_display_name(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({
            "value": [{"id": "AAMkAGI0", "displayName": "Billing"}]
        })
        client._mcp_client = mock
        found = client.mail_resolve_folder("billing")
        assert found["id"] == "AAMkAGI0"
        assert found["name"] == "Billing"

    def test_resolve_well_known_without_list(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        client._mcp_client = mock
        found = client.mail_resolve_folder("archive")
        assert found == {"id": "archive", "name": "archive", "well_known": True}
        mock.call_tool.assert_not_called()

    def test_resolve_long_display_name_not_mistaken_for_id(self, mcp_key, tmp_project):
        # A 26-char, space-free *display name* must resolve via the folder list,
        # not be silently treated as an opaque folder id (regression: the old
        # length>=20 heuristic short-circuited before the name lookup).
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({
            "value": [{"id": "AAMkREALID", "displayName": "Newsletters_and_Promotions"}]
        })
        client._mcp_client = mock
        found = client.mail_resolve_folder("Newsletters_and_Promotions")
        assert found["id"] == "AAMkREALID"
        assert found["name"] == "Newsletters_and_Promotions"
        assert found.get("well_known") is not True

    def test_resolve_opaque_id_falls_through_when_unlisted(self, mcp_key, tmp_project):
        # A long space-free token that is not a visible display name is assumed
        # to be a folder id.
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"value": []})
        client._mcp_client = mock
        found = client.mail_resolve_folder("AAMkAGI0longopaqueid1234567")
        assert found == {
            "id": "AAMkAGI0longopaqueid1234567",
            "name": "AAMkAGI0longopaqueid1234567",
            "well_known": False,
        }


class TestMailSendApprovalGated:
    def _client(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(_ms_workspace())

    def test_send_blocked_without_destructive_flag(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        client._mcp_client = mock
        os.environ.pop("CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE", None)
        res = client.mail_send("a@b.com", "Hi", "Body")
        assert res["success"] is False
        assert "approval" in (res.get("error") or "").lower() or "DESTRUCTIVE" in (
            res.get("error") or ""
        )
        mock.call_tool.assert_not_called()

    def test_send_executes_with_dual_gate(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({})
        client._mcp_client = mock
        os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = "1"
        # AUTO_APPROVE already set by mcp_key fixture
        res = client.mail_send("a@b.com", "Hi", "Body", cc="c@d.com")
        assert res["success"] is True
        assert res["action"] == "mail.send"
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "OUTLOOK_SEND_EMAIL"
        assert call["arguments"]["to"] == "a@b.com"
        assert call["arguments"]["subject"] == "Hi"
        assert call["arguments"]["body"] == "Body"
        assert call["arguments"]["cc_emails"] == ["c@d.com"]
        assert call["arguments"]["save_to_sent_items"] is True


class TestCapabilitiesPhase3:
    def test_composio_microsoft_phase3_and_send(self):
        from workspace_capabilities import get_capabilities, get_unsupported_reason, supports
        caps = get_capabilities("composio_microsoft:mcp")
        assert caps["mail.list_folders"] is True
        assert caps["mail.move"] is True
        assert caps["mail.send"] is True
        assert supports("composio_microsoft:mcp", "mail.send") is True
        # Google Composio still disabled for send
        assert get_capabilities("composio:mcp")["mail.send"] is False
        reason = get_unsupported_reason("composio:mcp", "gmail.send")
        assert "intentionally disabled" in reason
        # No intentional-disable entry for MS send anymore
        assert "intentionally disabled" not in get_unsupported_reason(
            "composio_microsoft:mcp", "mail.send"
        )

    def test_client_supports(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        assert client.supports("mail.send") is True
        assert client.supports("mail.list_folders") is True
        assert client.supports("mail.move") is True
        assert client.supports("mail.tag") is False
