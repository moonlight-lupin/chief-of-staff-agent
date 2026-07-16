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

    def test_mail_tag_resolves_display_name(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.side_effect = [
            _ok({"labels": [
                {"id": "Label_9", "name": "CoS-Verify", "type": "user"},
            ]}),
            _ok({"id": "abc123"}),
        ]
        client._mcp_client = mock
        res = client.mail_tag("abc123", "CoS-Verify")
        assert res["success"] is True
        assert res["data"]["label_id"] == "Label_9"
        modify = mock.call_tool.call_args_list[-1][0][1]["tools"][0]
        assert modify["arguments"]["add_label_ids"] == ["Label_9"]

    def test_reject_gmail_draft_id_on_tag_archive_trash(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        client._mcp_client = mock
        cases = [
            ("mail_tag", lambda: client.mail_tag("r-12345", "Label_9")),
            ("mail_archive", lambda: client.mail_archive("r-12345")),
            ("mail_unarchive", lambda: client.mail_unarchive("r-12345")),
            ("mail_trash", lambda: client.mail_trash("r-12345")),
            ("mail_untrash", lambda: client.mail_untrash("r-12345")),
        ]
        for name, call in cases:
            res = call()
            assert res["success"] is False, name
            assert "draft id" in (res.get("error") or "").lower(), name
        assert mock.call_tool.call_count == 0

    def test_create_tag_reuses_existing_label_id(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()

        def _side_effect(*args, **kwargs):
            tools = args[1]["tools"] if len(args) > 1 else kwargs.get("tools")
            slug = tools[0]["tool_slug"]
            if slug == "GMAIL_CREATE_LABEL":
                raise RuntimeError("Label already exists (409 conflict)")
            if slug == "GMAIL_LIST_LABELS":
                return _ok({"labels": [
                    {"id": "Label_77", "name": "CoS-Verify", "type": "user"},
                ]})
            return _ok({})

        mock.call_tool.side_effect = _side_effect
        client._mcp_client = mock
        res = client.mail_create_tag("CoS-Verify")
        assert res["success"] is True
        assert res["data"]["id"] == "Label_77"
        assert res["data"].get("reused") is True

    def test_create_draft_surfaces_message_id(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({
            "id": "r-999",
            "message": {"id": "19a0deadbeef"},
        })
        client._mcp_client = mock
        res = client.mail_create_draft("a@b.com", "Subj", "Body")
        assert res["success"] is True
        assert res["data"]["id"] == "19a0deadbeef"
        assert res["data"]["message_id"] == "19a0deadbeef"
        assert res["data"]["draft_id"] == "r-999"

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
    def test_caps_reflect_live_execution(self):
        # Execution-verified 2026-07-16 (live Gmail + Drive): list_tags, create_tag,
        # send, mail.tag/archive/unarchive/trash/untrash, and files.trash
        # (GOOGLEDRIVE_CREATE_FILE_FROM_TEXT → GOOGLEDRIVE_TRASH_FILE → confirmed in
        # Trash). files.upload is True (PR #14): text via CREATE_FILE_FROM_TEXT,
        # binary via GOOGLEDRIVE_UPLOAD_FILE + MCP sandbox staging (no
        # COMPOSIO_API_KEY). calendar.cancel / folders False.
        from workspace_capabilities import get_capabilities
        caps = get_capabilities("composio:mcp")
        for action in ("mail.list_tags", "mail.create_tag", "mail.send",
                       "mail.archive", "mail.unarchive", "mail.trash",
                       "mail.untrash", "mail.tag", "files.trash", "files.upload"):
            assert caps[action] is True, f"{action} should be True"
        assert caps["calendar.cancel"] is False
        assert caps["mail.list_folders"] is False

    def test_client_supports(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_google_workspace())
        assert client.supports("mail.list_tags") is True
        assert client.supports("mail.create_tag") is True
        assert client.supports("mail.send") is True
        assert client.supports("mail.archive") is True
        assert client.supports("mail.tag") is True
        assert client.supports("files.trash") is True
        assert client.supports("files.upload") is True   # MCP sandbox staging (PR #14)
        assert client.supports("calendar.cancel") is False

    def test_files_trash_google_slug(self, mcp_key, tmp_project):
        client = TestGoogleMailCleanup()._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({})
        client._mcp_client = mock
        res = client.files_trash("drive-file-1")
        assert res["success"] is True
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "GOOGLEDRIVE_TRASH_FILE"
        assert call["arguments"] == {"file_id": "drive-file-1"}

    def test_text_upload_uses_create_from_text_no_staging(self, mcp_key, tmp_project):
        # Text files go through GOOGLEDRIVE_CREATE_FILE_FROM_TEXT (MCP-native,
        # file_name+text_content) — no Files-API staging / COMPOSIO_API_KEY.
        from unittest.mock import patch
        client = TestGoogleMailCleanup()._client()
        local = tmp_project / "note.md"
        local.write_text("# hello\n", encoding="utf-8")
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "gdrive-text-1", "name": "note.md"})
        client._mcp_client = mock
        with patch("composio_files.stage_file_uploadable") as stage:
            res = client.files_upload(str(local), parent_id="folder-9")
        assert res["success"] is True
        stage.assert_not_called()   # text path must NOT stage
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "GOOGLEDRIVE_CREATE_FILE_FROM_TEXT"
        assert call["arguments"] == {
            "file_name": "note.md",
            "text_content": "# hello\n",
            "parent_id": "folder-9",
        }
