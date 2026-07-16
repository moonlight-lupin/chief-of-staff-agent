#!/usr/bin/env python3
"""v0.3.10 — Composio Files API staging for OneDrive FileUploadable uploads."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
for p in (SHARED_SCRIPTS, SHARED_SCRIPTS / "providers", PLUGIN_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import composio_files as cf  # noqa: E402


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else ("" if payload is None else str(payload))

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_stage_file_uploadable_posts_then_puts(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", "api-key-test")
    path = tmp_path / "note.txt"
    path.write_text("hello composio", encoding="utf-8")

    meta = {
        "key": "uploads/abc/note.txt",
        "new_presigned_url": "https://blob.example/put",
        "metadata": {"storage_backend": "azure_blob_storage"},
    }
    post = MagicMock(return_value=_Resp(200, meta))
    put = MagicMock(return_value=_Resp(201, text="created"))

    with patch.object(cf.requests, "post", post), patch.object(cf.requests, "put", put):
        result = cf.stage_file_uploadable(
            path,
            tool_slug="ONE_DRIVE_ONEDRIVE_UPLOAD_FILE",
            toolkit_slug="one_drive",
        )

    assert result == {
        "name": "note.txt",
        "mimetype": "text/plain",
        "s3key": "uploads/abc/note.txt",
    }
    post.assert_called_once()
    _, kwargs = post.call_args
    assert kwargs["headers"]["x-api-key"] == "api-key-test"
    assert kwargs["json"]["toolkit_slug"] == "one_drive"
    assert kwargs["json"]["tool_slug"] == "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE"
    assert kwargs["json"]["filename"] == "note.txt"
    assert "md5" in kwargs["json"]
    put.assert_called_once()
    put_args, put_kwargs = put.call_args
    assert put_args[0] == "https://blob.example/put"
    assert put_kwargs["headers"]["x-ms-blob-type"] == "BlockBlob"


def test_stage_file_uploadable_dedup_skips_put(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSIO_MCP_KEY", "mcp-key")
    path = tmp_path / "dup.bin"
    path.write_bytes(b"\x00\x01")
    meta = {"key": "uploads/dup.bin"}  # no presigned URL → dedup hit
    with patch.object(cf.requests, "post", return_value=_Resp(200, meta)) as post, \
         patch.object(cf.requests, "put") as put:
        result = cf.stage_file_uploadable(
            path,
            tool_slug="ONE_DRIVE_ONEDRIVE_UPLOAD_FILE",
            toolkit_slug="one_drive",
        )
    assert result["s3key"] == "uploads/dup.bin"
    post.assert_called_once()
    put.assert_not_called()


def test_stage_falls_back_to_v3_path(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", "k")
    path = tmp_path / "a.txt"
    path.write_text("x")
    responses = [
        _Resp(404, text="missing"),
        _Resp(200, {"key": "k1", "new_presigned_url": "https://s3/put"}),
    ]
    post = MagicMock(side_effect=responses)
    put = MagicMock(return_value=_Resp(200))
    with patch.object(cf.requests, "post", post), patch.object(cf.requests, "put", put):
        result = cf.stage_file_uploadable(
            path, tool_slug="T", toolkit_slug="one_drive",
        )
    assert result["s3key"] == "k1"
    assert post.call_count == 2
    assert "/api/v3.1/files/upload/request" in post.call_args_list[0][0][0]
    assert "/api/v3/files/upload/request" in post.call_args_list[1][0][0]


def test_find_s3url_nested():
    payload = {"data": {"content": {"s3url": "https://cdn.example/f"}}}
    assert cf.find_s3url(payload) == "https://cdn.example/f"


def test_download_s3url(tmp_path):
    out = tmp_path / "out.txt"
    resp = MagicMock()
    resp.status_code = 200
    resp.iter_content = lambda chunk_size: [b"abc", b"def"]
    with patch.object(cf.requests, "get", return_value=resp):
        path = cf.download_s3url("https://cdn.example/f", out)
    assert path.read_bytes() == b"abcdef"


def test_resolve_composio_api_key_prefers_api_key(monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", "api")
    monkeypatch.setenv("COMPOSIO_MCP_KEY", "mcp")
    assert cf.resolve_composio_api_key() == "api"
    monkeypatch.delenv("COMPOSIO_API_KEY")
    assert cf.resolve_composio_api_key() == "mcp"


def test_microsoft_client_text_upload_skips_staging(monkeypatch, tmp_path):
    """Plain-text → CREATE_TEXT_FILE over MCP (no Files API / COMPOSIO_API_KEY)."""
    monkeypatch.setenv("COMPOSIO_MCP_KEY", "test-key")
    monkeypatch.setenv("CHIEF_OF_STAFF_AUTO_APPROVE", "1")
    monkeypatch.setenv("CHIEF_OF_STAFF_PROJECT_ROOT", str(tmp_path))

    from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

    cfg = {
        "integrations": {
            "workspace": {
                "provider": "composio",
                "mode": "mcp",
                "family": "microsoft",
                "user_id": "u",
                "toolkits": ["outlook", "one_drive"],
                "mcp": {
                    "endpoint": "https://connect.composio.dev/mcp",
                    "key_env": "COMPOSIO_MCP_KEY",
                },
            }
        },
        "paths": {"project_root": str(tmp_path)},
    }
    client = ComposioMCPWorkspaceClient(cfg)
    local = tmp_path / "up.txt"
    local.write_text("payload")
    mock = MagicMock()
    mock.call_tool.return_value = {
        "data": {"results": [{"response": {"successful": True, "data": {"id": "file-9"}}}]}
    }
    client._mcp_client = mock

    with patch.object(client, "_ms_stage_file_uploadable") as stage:
        res = client.files_upload(str(local), parent_id="folder-1")

    assert res["success"] is True
    stage.assert_not_called()
    tool = mock.call_tool.call_args[0][1]["tools"][0]
    assert tool["tool_slug"] == "ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE"
    assert tool["arguments"] == {
        "name": "up.txt",
        "content": "payload",
        "folder": "folder-1",
    }


def test_microsoft_client_binary_stages_before_mcp_execute(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOSIO_MCP_KEY", "test-key")
    monkeypatch.setenv("CHIEF_OF_STAFF_AUTO_APPROVE", "1")
    monkeypatch.setenv("CHIEF_OF_STAFF_PROJECT_ROOT", str(tmp_path))

    from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

    cfg = {
        "integrations": {
            "workspace": {
                "provider": "composio",
                "mode": "mcp",
                "family": "microsoft",
                "user_id": "u",
                "toolkits": ["outlook", "one_drive"],
                "mcp": {
                    "endpoint": "https://connect.composio.dev/mcp",
                    "key_env": "COMPOSIO_MCP_KEY",
                },
            }
        },
        "paths": {"project_root": str(tmp_path)},
    }
    client = ComposioMCPWorkspaceClient(cfg)
    local = tmp_path / "up.bin"
    local.write_bytes(b"\x00\xff")
    staged = {
        "name": "up.bin",
        "mimetype": "application/octet-stream",
        "s3key": "uploads/up.bin",
    }
    mock = MagicMock()
    mock.call_tool.return_value = {
        "data": {"results": [{"response": {"successful": True, "data": {"id": "file-9"}}}]}
    }
    client._mcp_client = mock

    with patch.object(client, "_ms_stage_file_uploadable", return_value=staged) as stage:
        res = client.files_upload(str(local), parent_id="folder-1")

    assert res["success"] is True
    stage.assert_called_once()
    tool = mock.call_tool.call_args[0][1]["tools"][0]
    assert tool["tool_slug"] == "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE"
    assert tool["arguments"] == {"file": staged, "folder": "folder-1"}


def test_stage_retries_consumer_api_key_on_401(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSIO_MCP_KEY", "mcp-only")
    path = tmp_path / "x.bin"
    path.write_bytes(b"ab")
    meta = {
        "key": "uploads/x.bin",
        "new_presigned_url": "https://blob.example/put",
    }
    post = MagicMock(side_effect=[
        _Resp(401, text="unauthorized"),
        _Resp(200, meta),
    ])
    put = MagicMock(return_value=_Resp(201))
    with patch.object(cf.requests, "post", post), patch.object(cf.requests, "put", put):
        result = cf.stage_file_uploadable(
            path,
            tool_slug="ONE_DRIVE_ONEDRIVE_UPLOAD_FILE",
            toolkit_slug="one_drive",
        )
    assert result["s3key"] == "uploads/x.bin"
    assert post.call_count == 2
    assert post.call_args_list[0][1]["headers"].get("x-api-key") == "mcp-only"
    assert post.call_args_list[1][1]["headers"].get("x-consumer-api-key") == "mcp-only"
