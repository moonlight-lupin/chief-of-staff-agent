#!/usr/bin/env python3
"""v0.3.13 — Google Composio parity: archive/trash/tags/send."""
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
        "paths": {"project_root": "/tmp/test-google-parity"},
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


class TestGoogleFamilySlugs:
    def test_cleanup_and_tag_slugs(self):
        from providers.composio_mcp_workspace_base import FAMILY_SLUGS
        g = FAMILY_SLUGS["google"]
        assert g["mail_send"] == "GMAIL_SEND_EMAIL"
        assert g["mail_list_tags"] == "GMAIL_LIST_LABELS"
        assert g["mail_create_tag"] == "GMAIL_CREATE_LABEL"
        assert g["mail_modify_labels"] == "GMAIL_ADD_LABEL_TO_EMAIL"
        assert g["mail_trash"] == "GMAIL_MOVE_TO_TRASH"
        assert g["mail_untrash"] == "GMAIL_UNTRASH_MESSAGE"


class TestGoogleMailCleanup:
    def _client(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(_google_workspace())

    def test_list_tags(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({
            "labels": [
                {"id": "INBOX", "name": "INBOX", "type": "system"},
                {"id": "Label_9", "name": "Finance/Invoices", "type": "user"},
            ]
        })
        client._mcp_client = mock
        tags = client.mail_list_tags()
        assert tags == [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "Label_9", "name": "Finance/Invoices", "type": "user"},
        ]
        assert mock.call_tool.call_args[0][1]["tools"][0]["tool_slug"] == "GMAIL_LIST_LABELS"

    def test_create_tag(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "Label_42", "name": "CoS-Verify"})
        client._mcp_client = mock
        res = client.mail_create_tag("CoS-Verify")
        assert res["success"] is True
        assert res["data"]["id"] == "Label_42"
        args = mock.call_tool.call_args[0][1]["tools"][0]["arguments"]
        assert args == {"label_name": "CoS-Verify"}

    def test_mail_tag_adds_label_id(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "msg-1"})
        client._mcp_client = mock
        res = client.mail_tag("msg-1", "Label_9")
        assert res["success"] is True
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "GMAIL_ADD_LABEL_TO_EMAIL"
        assert call["arguments"] == {
            "message_id": "msg-1",
            "add_label_ids": ["Label_9"],
        }

    def test_archive_removes_inbox(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "msg-1"})
        client._mcp_client = mock
        res = client.mail_archive("msg-1")
        assert res["success"] is True
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "GMAIL_ADD_LABEL_TO_EMAIL"
        assert call["arguments"]["remove_label_ids"] == ["INBOX"]

    def test_unarchive_adds_inbox(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "msg-1"})
        client._mcp_client = mock
        client.mail_unarchive("msg-1")
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["arguments"]["add_label_ids"] == ["INBOX"]

    def test_trash_and_untrash(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "msg-1"})
        client._mcp_client = mock
        assert client.mail_trash("msg-1")["success"] is True
        assert mock.call_tool.call_args[0][1]["tools"][0]["tool_slug"] == "GMAIL_MOVE_TO_TRASH"
        assert client.mail_untrash("msg-1")["success"] is True
        assert mock.call_tool.call_args[0][1]["tools"][0]["tool_slug"] == "GMAIL_UNTRASH_MESSAGE"

    def test_send_approval_gated(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "sent-1"})
        client._mcp_client = mock
        # Without ALLOW_DESTRUCTIVE, send is blocked.
        res = client.mail_send("a@b.com", "Hi", "Body")
        assert res["success"] is False
        os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = "1"
        res = client.mail_send("a@b.com", "Hi", "Body", cc="c@d.com")
        assert res["success"] is True
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "GMAIL_SEND_EMAIL"
        assert call["arguments"]["recipient_email"] == "a@b.com"
        assert call["arguments"]["cc"] == ["c@d.com"]


class TestGoogleCapabilities:
    def test_caps_flipped(self):
        from workspace_capabilities import get_capabilities
        caps = get_capabilities("composio:mcp")
        assert caps["mail.archive"] is True
        assert caps["mail.trash"] is True
        assert caps["mail.unarchive"] is True
        assert caps["mail.untrash"] is True
        assert caps["mail.list_tags"] is True
        assert caps["mail.tag"] is True
        assert caps["mail.create_tag"] is True
        assert caps["mail.send"] is True
        assert caps["calendar.cancel"] is False
        assert caps["files.trash"] is False
        assert caps["mail.list_folders"] is False

    def test_client_supports(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_google_workspace())
        assert client.supports("mail.archive") is True
        assert client.supports("mail.list_tags") is True
        assert client.supports("mail.send") is True
        assert client.supports("calendar.cancel") is False
