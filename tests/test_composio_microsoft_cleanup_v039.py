#!/usr/bin/env python3
"""v0.3.9 — Composio Microsoft cleanup primitives (Phase 1).

Proves archive/trash/unarchive/untrash + OneDrive soft-delete:
  * FAMILY_SLUGS bind OUTLOOK_MOVE_MESSAGE / ONE_DRIVE_DELETE_ITEM,
  * move args use message_id + destination_id (well-known folders),
  * ActionResult shapes match native m365 (restore_target, reversible),
  * google-family clients refuse cleanup with NotImplementedError,
  * capability matrix advertises cleanup so --verify-writes is unblocked.

All MCP calls are mocked — no network.
"""
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
        "mcp": {"endpoint": "https://connect.composio.dev/mcp", "key_env": "COMPOSIO_MCP_KEY"},
    }
    ws.update(extra)
    return {"integrations": {"workspace": ws}, "paths": {"project_root": "/tmp/test-ms-cleanup"}}


def _google_workspace():
    return {
        "integrations": {
            "workspace": {
                "provider": "composio",
                "mode": "mcp",
                "user_id": "test-user",
                "toolkits": ["gmail", "googlecalendar", "googledrive"],
                "mcp": {
                    "endpoint": "https://connect.composio.dev/mcp",
                    "key_env": "COMPOSIO_MCP_KEY",
                },
            }
        },
        "paths": {"project_root": "/tmp/test-g-cleanup"},
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


@pytest.fixture
def tmp_project():
    with tempfile.TemporaryDirectory() as d:
        os.environ["CHIEF_OF_STAFF_PROJECT_ROOT"] = d
        yield Path(d)
        os.environ.pop("CHIEF_OF_STAFF_PROJECT_ROOT", None)


class TestFamilySlugs:
    def test_microsoft_cleanup_slugs(self):
        from providers.composio_mcp_workspace_base import FAMILY_SLUGS
        ms = FAMILY_SLUGS["microsoft"]
        assert ms["mail_move"] == "OUTLOOK_MOVE_MESSAGE"
        assert ms["files_trash"] == "ONE_DRIVE_DELETE_ITEM"
        assert "mail_move" not in FAMILY_SLUGS["google"]
        assert "files_trash" not in FAMILY_SLUGS["google"]


class TestMailMoveCleanup:
    def _client(self, **extra):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(_ms_workspace(**extra))

    def test_trash_moves_to_deleteditems(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "moved-trash"})
        client._mcp_client = mock

        res = client.mail_trash("msg-1")

        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "OUTLOOK_MOVE_MESSAGE"
        assert call["arguments"] == {
            "message_id": "msg-1",
            "destination_id": "deleteditems",
        }
        assert res["success"] is True
        assert res["action"] == "mail.trash"
        assert res["data"]["destination"] == "deleteditems"
        assert res["data"]["restore_target"] == "moved-trash"
        assert res["data"]["reversible"] is True
        assert res["tool_slug"] == "OUTLOOK_MOVE_MESSAGE"

    def test_archive_moves_to_archive(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "moved-arch"})
        client._mcp_client = mock

        res = client.mail_archive("msg-2")

        args = mock.call_tool.call_args[0][1]["tools"][0]["arguments"]
        assert args["destination_id"] == "archive"
        assert res["data"]["restore_target"] == "moved-arch"

    def test_unarchive_and_untrash_restore_to_inbox(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "back"})
        client._mcp_client = mock

        ua = client.mail_unarchive("msg-3")
        assert mock.call_tool.call_args[0][1]["tools"][0]["arguments"]["destination_id"] == "inbox"
        assert ua["action"] == "mail.unarchive"

        ut = client.mail_untrash("msg-4")
        assert mock.call_tool.call_args[0][1]["tools"][0]["arguments"]["destination_id"] == "inbox"
        assert ut["action"] == "mail.untrash"
        assert ut["data"]["restore_target"] == "back"

    def test_tool_slugs_override_mail_move(self, mcp_key, tmp_project):
        client = self._client(tool_slugs={"mail_move": "OUTLOOK_CUSTOM_MOVE"})
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "x"})
        client._mcp_client = mock

        res = client.mail_trash("m")
        assert mock.call_tool.call_args[0][1]["tools"][0]["tool_slug"] == "OUTLOOK_CUSTOM_MOVE"
        assert res["tool_slug"] == "OUTLOOK_CUSTOM_MOVE"

    def test_nested_id_extraction(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"data": {"id": "nested-id"}})
        client._mcp_client = mock

        res = client.mail_trash("orig")
        assert res["data"]["id"] == "nested-id"
        assert res["data"]["restore_target"] == "nested-id"

    def test_google_family_refuses_mail_trash(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_google_workspace())
        mock = MagicMock()
        client._mcp_client = mock

        res = client.mail_trash("msg")
        assert res["success"] is False
        assert "Microsoft" in (res.get("error") or "")
        mock.call_tool.assert_not_called()


class TestFilesTrash:
    def test_files_trash_uses_soft_delete_slug(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _ok({})
        client._mcp_client = mock

        res = client.files_trash("file-1")

        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "ONE_DRIVE_DELETE_ITEM"
        assert call["arguments"] == {"item_id": "file-1"}
        assert res["success"] is True
        assert res["action"] == "files.trash"
        assert res["data"] == {"id": "file-1", "reversible": True}

    def test_google_family_refuses_files_trash(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_google_workspace())
        mock = MagicMock()
        client._mcp_client = mock

        res = client.files_trash("f1")
        assert res["success"] is False
        assert "Microsoft" in (res.get("error") or "")
        mock.call_tool.assert_not_called()


class TestCapabilitiesPhase1And2:
    # v0.3.10: files staging + archive/inbox moves are capability-True alongside
    # the v0.3.9 live-verified draft/trash/calendar writes.
    def test_cleanup_and_content_writes_live_verified(self):
        from workspace_capabilities import get_capabilities, supports
        caps = get_capabilities("composio_microsoft:mcp")
        assert caps["mail.trash"] is True
        assert caps["mail.draft"] is True
        assert caps["calendar.create"] is True
        assert caps["calendar.update"] is True
        assert caps["calendar.delete"] is True
        assert supports("composio_microsoft:mcp", "gmail.trash") is True
        assert caps["mail.archive"] is True
        assert caps["mail.untrash"] is True
        assert caps["mail.unarchive"] is True
        assert caps["files.trash"] is True
        assert caps["files.upload"] is True
        assert caps["files.download"] is True
        assert supports("composio_microsoft:mcp", "drive.trash") is True

    def test_client_supports_cleanup_and_writes(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        assert client.supports("mail.trash") is True
        assert client.supports("mail.draft") is True
        assert client.supports("files.trash") is True
        assert client.supports("files.upload") is True
        assert client.supports("mail.archive") is True


class TestVerifyWritesPhase2:
    """--verify-writes exercises draft, mail-move cycle, and OneDrive files."""

    def test_verify_writes_draft_move_and_files(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        from workspace_verify import run_verification

        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        staged = {
            "name": "cos-verify.txt",
            "mimetype": "text/plain",
            "s3key": "uploads/test/cos-verify.txt",
        }

        def _side_effect(tool_name, payload):
            slug = payload["tools"][0]["tool_slug"]
            args = payload["tools"][0].get("arguments") or {}
            if slug == "OUTLOOK_CREATE_DRAFT":
                return _ok({"id": "draft-1"})
            if slug == "OUTLOOK_MOVE_MESSAGE":
                dest = args.get("destination_id") or args.get("destinationFolderId")
                return _ok({"id": f"draft-{dest or 'moved'}", "restore_target": "draft-1"})
            if slug == "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE":
                assert isinstance(args.get("file"), dict)
                assert args["file"].get("s3key") == staged["s3key"]
                return _ok({"id": "file-1"})
            if slug == "ONE_DRIVE_DOWNLOAD_FILE":
                return _ok({
                    "id": "file-1",
                    "content": {"s3url": "https://example.test/cos-verify.txt"},
                })
            if slug == "ONE_DRIVE_DELETE_ITEM":
                return _ok({"id": "file-1"})
            if slug in (
                "OUTLOOK_QUERY_EMAILS",
                "OUTLOOK_GET_CALENDAR_VIEW",
                "ONE_DRIVE_SEARCH_ITEMS",
            ):
                return _ok({"value": []})
            return _ok({})

        mock.call_tool.side_effect = _side_effect
        mock.initialize.return_value = None
        client._mcp_client = mock

        with patch.object(client, "_ms_stage_file_uploadable", return_value=staged), \
             patch("composio_files.download_s3url") as dl, \
             patch("workspace_verify.get_workspace_client", return_value=client):
            dl.side_effect = lambda url, path, **kw: Path(path).write_text("ok")
            cfg = _ms_workspace()
            cfg["user"] = {"email": "op@example.com"}
            rep = run_verification(cfg, include_writes=True)

        assert rep["checks"]["mail_draft"]["status"] == "pass"
        assert rep["checks"]["mail_tag_write"]["status"] == "not_tested"
        assert rep["checks"]["mail_move_write"]["status"] == "pass"
        assert rep["checks"]["files_write"]["status"] == "pass"
        assert rep["write_ready"] == "yes"
        slugs = [
            c[0][1]["tools"][0]["tool_slug"]
            for c in mock.call_tool.call_args_list
            if c[0][0] == "COMPOSIO_MULTI_EXECUTE_TOOL"
        ]
        assert "OUTLOOK_CREATE_DRAFT" in slugs
        assert "OUTLOOK_MOVE_MESSAGE" in slugs
        assert "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE" in slugs
        assert "ONE_DRIVE_DELETE_ITEM" in slugs

    def test_calendar_write_opt_in_create_update_delete(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        from workspace_verify import run_verification

        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()

        def _side_effect(tool_name, payload):
            slug = payload["tools"][0]["tool_slug"]
            if slug == "OUTLOOK_CALENDAR_CREATE_EVENT":
                return _ok({"id": "evt-1"})
            if slug == "OUTLOOK_UPDATE_CALENDAR_EVENT":
                return _ok({"id": "evt-1"})
            if slug == "OUTLOOK_DELETE_CALENDAR_EVENT":
                return _ok({})
            if slug in (
                "OUTLOOK_QUERY_EMAILS",
                "OUTLOOK_GET_CALENDAR_VIEW",
                "ONE_DRIVE_SEARCH_ITEMS",
            ):
                return _ok({"value": []})
            return _ok({})

        mock.call_tool.side_effect = _side_effect
        mock.initialize.return_value = None
        client._mcp_client = mock

        with patch("workspace_verify.get_workspace_client", return_value=client):
            rep = run_verification(
                _ms_workspace(),
                include_writes=False,
                include_calendar_writes=True,
            )

        assert rep["checks"]["calendar_write"]["status"] == "pass"
        assert rep["write_ready"] == "yes"
        slugs = [
            c[0][1]["tools"][0]["tool_slug"]
            for c in mock.call_tool.call_args_list
            if c[0][0] == "COMPOSIO_MULTI_EXECUTE_TOOL"
        ]
        assert "OUTLOOK_CALENDAR_CREATE_EVENT" in slugs
        assert "OUTLOOK_UPDATE_CALENDAR_EVENT" in slugs
        assert "OUTLOOK_DELETE_CALENDAR_EVENT" in slugs
