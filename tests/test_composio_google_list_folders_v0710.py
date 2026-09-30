#!/usr/bin/env python3
"""v0.7.10 — Composio Google mail.list_folders aliases GMAIL_LIST_LABELS."""
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
        "paths": {"project_root": "/tmp/test-google-list-folders"},
    }


def _ok(data):
    return {"data": {"results": [{"response": {"successful": True, "data": data}}]}}


@pytest.fixture
def mcp_key():
    os.environ["COMPOSIO_MCP_KEY"] = "test-key"
    yield
    os.environ.pop("COMPOSIO_MCP_KEY", None)


@pytest.fixture
def tmp_project():
    with tempfile.TemporaryDirectory() as d:
        os.environ["CHIEF_OF_STAFF_PROJECT_ROOT"] = d
        yield Path(d)
        os.environ.pop("CHIEF_OF_STAFF_PROJECT_ROOT", None)


class TestGoogleListFoldersSlug:
    def test_google_slug_matches_list_tags(self):
        from providers.composio_mcp_workspace_base import FAMILY_SLUGS
        g = FAMILY_SLUGS["google"]
        assert g["mail_list_folders"] == "GMAIL_LIST_LABELS"
        assert g["mail_list_folders"] == g["mail_list_tags"]


class TestGoogleListFoldersClient:
    def _client(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(_google_workspace())

    def test_list_folders_does_not_require_microsoft(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({
            "labels": [
                {"id": "Label_42", "name": "Filed", "type": "user"},
            ]
        })
        client._mcp_client = mock
        folders = client.mail_list_folders(include_hidden=True, max_results=5)
        assert folders == [
            {"id": "Label_42", "name": "Filed", "type": "user"},
        ]
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "GMAIL_LIST_LABELS"
        assert "include_hidden" not in (call.get("arguments") or {})

    def test_label_ids_usable_for_move_resolution(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.side_effect = [
            _ok({"labels": [
                {"id": "Label_9", "name": "CoS-Verify", "type": "user"},
            ]}),
            _ok({}),
        ]
        client._mcp_client = mock
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        try:
            folders = client.mail_list_folders()
            assert folders[0]["id"] == "Label_9"
            res = client.mail_move_to_folder("msg-hex", folders[0]["id"])
            assert res["success"] is True
            move = mock.call_tool.call_args_list[-1][0][1]["tools"][0]
            assert move["tool_slug"] == "GMAIL_BATCH_MODIFY_MESSAGES"
            assert move["arguments"]["add_label_ids"] == ["Label_9"]
        finally:
            os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)


class TestGoogleListFoldersCapabilities:
    def test_caps_true_on_composio_google(self):
        from workspace_capabilities import get_capabilities, supports
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        for provider in ("composio", "composio:mcp"):
            caps = get_capabilities(provider)
            assert caps["mail.list_folders"] is True
            assert supports(provider, "mail.list_folders") is True

        caps_ga = get_capabilities("google_api")
        assert caps_ga["mail.list_folders"] is False

        client = ComposioMCPWorkspaceClient(_google_workspace())
        assert client.supports("mail.list_folders") is True
