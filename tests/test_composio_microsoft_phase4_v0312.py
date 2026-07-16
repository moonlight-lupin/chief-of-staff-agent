#!/usr/bin/env python3
"""v0.3.12 — Composio Microsoft Phase 4 categories + MCP-native OneDrive text upload."""
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
        "paths": {"project_root": "/tmp/test-ms-phase4"},
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


class TestFamilySlugsPhase4:
    def test_microsoft_tag_and_upload_slugs(self):
        from providers.composio_mcp_workspace_base import FAMILY_SLUGS
        ms = FAMILY_SLUGS["microsoft"]
        assert ms["mail_list_tags"] == "OUTLOOK_GET_MASTER_CATEGORIES"
        assert ms["mail_create_tag"] == "OUTLOOK_CREATE_USER_MASTER_CATEGORY"
        assert ms["mail_get_message"] == "OUTLOOK_GET_MESSAGE"
        assert ms["mail_update"] == "OUTLOOK_UPDATE_EMAIL"
        assert ms["files_upload"] == "ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE"
        assert ms["files_upload_binary"] == "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE"


class TestMailTagsPhase4:
    def _client(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(_ms_workspace())

    def test_list_tags_normalizes_display_name_as_id(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({
            "value": [
                {"id": "cat-guid-1", "displayName": "CoS-Verify", "color": "preset0"},
                {"id": "cat-guid-2", "displayName": "Follow Up", "color": "preset1"},
            ]
        })
        client._mcp_client = mock
        tags = client.mail_list_tags()
        assert tags == [
            {
                "id": "CoS-Verify",
                "name": "CoS-Verify",
                "displayName": "CoS-Verify",
                "color": "preset0",
                "graph_id": "cat-guid-1",
            },
            {
                "id": "Follow Up",
                "name": "Follow Up",
                "displayName": "Follow Up",
                "color": "preset1",
                "graph_id": "cat-guid-2",
            },
        ]
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "OUTLOOK_GET_MASTER_CATEGORIES"

    def test_create_tag_uses_display_name(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "guid-9", "displayName": "CoS-Verify"})
        client._mcp_client = mock
        res = client.mail_create_tag("CoS-Verify")
        assert res["success"] is True
        assert res["data"]["id"] == "CoS-Verify"
        assert res["data"]["graph_id"] == "guid-9"
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "OUTLOOK_CREATE_USER_MASTER_CATEGORY"
        assert call["arguments"]["display_name"] == "CoS-Verify"
        assert call["arguments"]["color"] == "preset0"

    def test_mail_tag_appends_categories(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()

        def _side_effect(tool_name, payload):
            slug = payload["tools"][0]["tool_slug"]
            if slug == "OUTLOOK_GET_MESSAGE":
                return _ok({"id": "msg-1", "categories": ["Existing"]})
            if slug == "OUTLOOK_UPDATE_EMAIL":
                return _ok({"id": "msg-1"})
            return _ok({})

        mock.call_tool.side_effect = _side_effect
        client._mcp_client = mock
        res = client.mail_tag("msg-1", "CoS-Verify")
        assert res["success"] is True
        assert res["data"]["categories"] == ["Existing", "CoS-Verify"]
        slugs = [
            c[0][1]["tools"][0]["tool_slug"]
            for c in mock.call_tool.call_args_list
            if c[0][0] == "COMPOSIO_MULTI_EXECUTE_TOOL"
        ]
        assert slugs == ["OUTLOOK_GET_MESSAGE", "OUTLOOK_UPDATE_EMAIL"]
        update_args = mock.call_tool.call_args_list[-1][0][1]["tools"][0]["arguments"]
        assert update_args == {
            "message_id": "msg-1",
            "categories": ["Existing", "CoS-Verify"],
        }

    def test_mail_tag_idempotent_when_already_present(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()

        def _side_effect(tool_name, payload):
            slug = payload["tools"][0]["tool_slug"]
            if slug == "OUTLOOK_GET_MESSAGE":
                return _ok({"categories": ["CoS-Verify"]})
            if slug == "OUTLOOK_UPDATE_EMAIL":
                return _ok({"id": "msg-1"})
            return _ok({})

        mock.call_tool.side_effect = _side_effect
        client._mcp_client = mock
        res = client.mail_tag("msg-1", "CoS-Verify")
        assert res["data"]["categories"] == ["CoS-Verify"]
        update_args = mock.call_tool.call_args_list[-1][0][1]["tools"][0]["arguments"]
        assert update_args["categories"] == ["CoS-Verify"]


class TestOneDriveTextUploadPhase4:
    def _client(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(_ms_workspace())

    def test_text_upload_uses_create_text_file_without_staging(
        self, mcp_key, tmp_project
    ):
        client = self._client()
        local = tmp_project / "note.md"
        local.write_text("# hello\n", encoding="utf-8")
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "file-text-1", "name": "note.md"})
        client._mcp_client = mock

        with patch.object(client, "_ms_stage_file_uploadable") as stage:
            res = client.files_upload(str(local), parent_id="/Notes")

        assert res["success"] is True
        assert res["data"]["id"] == "file-text-1"
        assert res["data"]["upload_slug"] == "ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE"
        stage.assert_not_called()
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE"
        assert call["arguments"] == {
            "name": "note.md",
            "content": "# hello\n",
            "folder": "/Notes",
        }

    def test_binary_upload_stages_file_uploadable(self, mcp_key, tmp_project):
        client = self._client()
        local = tmp_project / "blob.bin"
        local.write_bytes(b"\x00\x01\x02\xff")
        staged = {
            "name": "blob.bin",
            "mimetype": "application/octet-stream",
            "s3key": "uploads/blob.bin",
        }
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "file-bin-1"})
        client._mcp_client = mock

        with patch.object(client, "_ms_stage_file_uploadable", return_value=staged) as stage:
            res = client.files_upload(str(local), parent_id="folder-9")

        assert res["success"] is True
        assert res["data"]["upload_slug"] == "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE"
        stage.assert_called_once()
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE"
        assert call["arguments"] == {"file": staged, "folder": "folder-9"}


class TestCapabilitiesPhase4:
    def test_composio_microsoft_phase4_caps(self):
        from workspace_capabilities import get_capabilities
        caps = get_capabilities("composio_microsoft:mcp")
        assert caps["mail.list_tags"] is True
        assert caps["mail.tag"] is True
        assert caps["mail.create_tag"] is True
        # files.upload stays False: text works over MCP but binary needs
        # COMPOSIO_API_KEY (the headline document-filing case). download/trash
        # are verified and unaffected.
        assert caps["files.upload"] is False
        assert caps["files.download"] is True
        assert caps["files.trash"] is True
        assert caps["calendar.cancel"] is False

    def test_client_supports_phase4(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        assert client.supports("mail.list_tags") is True
        assert client.supports("mail.tag") is True
        assert client.supports("mail.create_tag") is True
        assert client.supports("files.upload") is False
        assert client.supports("files.download") is True
        assert client.supports("files.trash") is True
