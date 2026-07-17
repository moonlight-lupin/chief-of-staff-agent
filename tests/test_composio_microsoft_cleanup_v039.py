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
        assert ms["files_untrash"] == "ONE_DRIVE_RESTORE_DRIVE_ITEM"
        assert ms["files_get"] == "ONE_DRIVE_GET_ITEM"
        assert ms["files_recycle_list"] == "SHARE_POINT_LIST_RECYCLE_BIN_ITEMS"
        assert ms["files_recycle_restore"] == "SHARE_POINT_RESTORE_RECYCLE_BIN_ITEM"
        assert "mail_move" not in FAMILY_SLUGS["google"]
        assert FAMILY_SLUGS["google"]["files_trash"] == "GOOGLEDRIVE_TRASH_FILE"
        assert FAMILY_SLUGS["google"]["files_untrash"] == "GOOGLEDRIVE_UNTRASH_FILE"


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

    def test_google_family_mail_trash_uses_gmail_slug(self, mcp_key, tmp_project):
        # v0.3.13: Google Composio wires GMAIL_MOVE_TO_TRASH (no longer MS-only).
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_google_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "msg"})
        client._mcp_client = mock

        res = client.mail_trash("msg")
        assert res["success"] is True
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "GMAIL_MOVE_TO_TRASH"
        assert call["arguments"] == {"message_id": "msg"}


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

    def test_google_family_files_trash_uses_drive_slug(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_google_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _ok({})
        client._mcp_client = mock

        res = client.files_trash("f1")
        assert res["success"] is True
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "GOOGLEDRIVE_TRASH_FILE"
        assert call["arguments"] == {"file_id": "f1"}
        assert res["data"] == {"id": "f1", "reversible": True}

    def test_files_untrash_uses_restore_slug(self, mcp_key, tmp_project):
        """Personal path uses ONE_DRIVE_RESTORE_DRIVE_ITEM."""
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _ok({})
        client._mcp_client = mock

        res = client.files_untrash("file-1")

        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "ONE_DRIVE_RESTORE_DRIVE_ITEM"
        assert call["arguments"] == {"item_id": "file-1"}
        assert res["success"] is True
        assert res["action"] == "files.untrash"
        assert res["data"]["id"] == "file-1"
        assert res["data"]["trashed"] is False
        assert res["data"]["restore_path"] == "onedrive_personal"

    def test_files_untrash_guid_uses_sharepoint_recycle_bin(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _ok({})
        client._mcp_client = mock
        guid = "5d625d33-338c-4a77-a98a-3e287116440c"

        res = client.files_untrash(guid)

        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "SHARE_POINT_RESTORE_RECYCLE_BIN_ITEM"
        assert call["arguments"] == {"recyclebinitemid": guid}
        assert res["success"] is True
        assert res["data"]["restore_path"] == "sharepoint_recycle_bin"

    def test_files_trash_persists_recycle_bin_restore_target(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        guid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        def _tool_response(name, payload):
            slug = payload["tools"][0]["tool_slug"]
            if slug == "ONE_DRIVE_GET_ITEM":
                return _ok({"name": "notes.txt", "id": "file-1"})
            if slug == "SHARE_POINT_LIST_RECYCLE_BIN_ITEMS":
                return _ok({"value": [
                    {"Id": guid, "LeafName": "notes.txt", "Title": "notes.txt"},
                ]})
            return _ok({})

        mock = MagicMock()
        mock.call_tool.side_effect = _tool_response
        client._mcp_client = mock

        res = client.files_trash("file-1")
        assert res["success"] is True
        assert res["data"]["name"] == "notes.txt"
        assert res["data"]["restore_target"] == guid
        slugs = [c[0][1]["tools"][0]["tool_slug"] for c in mock.call_tool.call_args_list]
        assert "ONE_DRIVE_GET_ITEM" in slugs
        assert "ONE_DRIVE_DELETE_ITEM" in slugs
        assert "SHARE_POINT_LIST_RECYCLE_BIN_ITEMS" in slugs


class TestCapabilitiesPhase1And2:
    # v0.3.12: tags + OneDrive download/trash advertise True (Outlook categories /
    # DOWNLOAD_FILE / DELETE_ITEM, execution-verified). files.upload stays False:
    # text works over MCP but binary document filing needs COMPOSIO_API_KEY.
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
        assert caps["files.upload"] is True    # text + binary via MCP sandbox staging (PR #14, no COMPOSIO_API_KEY)
        assert caps["files.download"] is True
        assert supports("composio_microsoft:mcp", "drive.trash") is True
        assert caps["files.untrash"] is True
        assert supports("composio_microsoft:mcp", "drive.untrash") is True

    def test_client_supports_cleanup_and_writes(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        assert client.supports("mail.trash") is True
        assert client.supports("mail.draft") is True
        assert client.supports("mail.archive") is True
        assert client.supports("files.trash") is True
        assert client.supports("files.upload") is True   # MCP sandbox staging (PR #14)


class TestVerifyWritesPhase2:
    """--verify-writes exercises draft, tags, mail-move cycle, and OneDrive files."""

    def test_verify_writes_draft_tags_move_and_files(self, mcp_key, tmp_project):
        # files.upload is False by default (binary needs COMPOSIO_API_KEY). This
        # test covers the harness orchestration for the *supported* path — the
        # MCP-native text upload — by forcing files support and mocking staging,
        # so files_write exercises CREATE_TEXT_FILE end-to-end. Tags use master
        # categories + UPDATE_EMAIL.
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        from workspace_verify import run_verification

        client = ComposioMCPWorkspaceClient(_ms_workspace())
        _real_supports = client.supports

        def _force_files_supported(action):
            if action in ("files.upload", "files.download", "files.trash",
                          "drive.upload", "drive.download", "drive.trash"):
                return True
            return _real_supports(action)

        client.supports = _force_files_supported  # type: ignore[assignment]
        mock = MagicMock()
        draft_n = {"n": 0}

        def _side_effect(tool_name, payload):
            slug = payload["tools"][0]["tool_slug"]
            args = payload["tools"][0].get("arguments") or {}
            if slug == "OUTLOOK_CREATE_DRAFT":
                draft_n["n"] += 1
                return _ok({"id": f"draft-{draft_n['n']}"})
            if slug == "OUTLOOK_CREATE_USER_MASTER_CATEGORY":
                return _ok({"id": "cat-1", "displayName": args.get("display_name")})
            if slug == "OUTLOOK_GET_MESSAGE":
                return _ok({"id": args.get("message_id"), "categories": []})
            if slug == "OUTLOOK_UPDATE_EMAIL":
                return _ok({"id": args.get("message_id"), "categories": args.get("categories")})
            if slug == "OUTLOOK_MOVE_MESSAGE":
                dest = args.get("destination_id") or args.get("destinationFolderId")
                return _ok({"id": f"draft-{dest or 'moved'}", "restore_target": "draft-1"})
            if slug == "ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE":
                assert "content" in args and "name" in args
                return _ok({"id": "file-1", "name": args["name"]})
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

        with patch.object(client, "_ms_stage_file_uploadable") as stage, \
             patch("composio_files.download_s3url") as dl, \
             patch("workspace_verify.get_workspace_client", return_value=client):
            dl.side_effect = lambda url, path, **kw: Path(path).write_text("ok")
            cfg = _ms_workspace()
            cfg["user"] = {"email": "op@example.com"}
            rep = run_verification(cfg, include_writes=True)

        assert rep["checks"]["mail_draft"]["status"] == "pass"
        assert rep["checks"]["mail_tag_write"]["status"] == "pass"
        assert rep["checks"]["mail_move_write"]["status"] == "pass"
        assert rep["checks"]["files_write"]["status"] == "pass"
        assert rep["write_ready"] == "yes"
        stage.assert_not_called()
        slugs = [
            c[0][1]["tools"][0]["tool_slug"]
            for c in mock.call_tool.call_args_list
            if c[0][0] == "COMPOSIO_MULTI_EXECUTE_TOOL"
        ]
        assert "OUTLOOK_CREATE_DRAFT" in slugs
        assert "OUTLOOK_CREATE_USER_MASTER_CATEGORY" in slugs
        assert "OUTLOOK_UPDATE_EMAIL" in slugs
        assert "OUTLOOK_MOVE_MESSAGE" in slugs
        assert "ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE" in slugs
        assert "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE" not in slugs
        assert "ONE_DRIVE_DELETE_ITEM" in slugs

    def test_verify_writes_runs_files_via_text_path(self, mcp_key, tmp_project):
        # PR #14 reality (files.upload True): files_write RUNS. The verify harness
        # uploads a throwaway .txt, which takes the MCP-native text path
        # (ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE — no sandbox staging), then trashes
        # it via ONE_DRIVE_DELETE_ITEM. Draft + tags + mail-move also run.
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        from workspace_verify import run_verification

        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        draft_n = {"n": 0}

        def _side_effect(tool_name, payload):
            slug = payload["tools"][0]["tool_slug"]
            args = payload["tools"][0].get("arguments") or {}
            if slug == "OUTLOOK_CREATE_DRAFT":
                draft_n["n"] += 1
                return _ok({"id": f"draft-{draft_n['n']}"})
            if slug == "OUTLOOK_CREATE_USER_MASTER_CATEGORY":
                return _ok({"id": "cat-1", "displayName": "CoS-Verify"})
            if slug == "OUTLOOK_GET_MESSAGE":
                return _ok({"categories": []})
            if slug == "OUTLOOK_UPDATE_EMAIL":
                return _ok({"id": "draft-1"})
            if slug == "OUTLOOK_MOVE_MESSAGE":
                dest = args.get("destination_id") or args.get("destinationFolderId")
                return _ok({"id": f"draft-{dest or 'moved'}", "restore_target": "draft-1"})
            if slug == "ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE":
                return _ok({"id": "file-1"})
            if slug == "ONE_DRIVE_DELETE_ITEM":
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
            cfg = _ms_workspace()
            cfg["user"] = {"email": "op@example.com"}
            rep = run_verification(cfg, include_writes=True)

        assert rep["checks"]["mail_draft"]["status"] == "pass"
        assert rep["checks"]["mail_tag_write"]["status"] == "pass"
        assert rep["checks"]["mail_move_write"]["status"] == "pass"
        assert rep["checks"]["files_write"]["status"] == "pass"
        assert rep["write_ready"] == "yes"
        slugs = [
            c[0][1]["tools"][0]["tool_slug"]
            for c in mock.call_tool.call_args_list
            if c[0][0] == "COMPOSIO_MULTI_EXECUTE_TOOL"
        ]
        # The verify harness uploads a .txt → text path, not binary staging.
        assert "ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE" in slugs
        assert "ONE_DRIVE_DELETE_ITEM" in slugs
        assert "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE" not in slugs

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
