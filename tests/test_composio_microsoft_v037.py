#!/usr/bin/env python3
"""v0.3.7 — Composio Microsoft 365 toolkit family.

Proves the family-selectable Composio provider:
  * family selection (explicit / inferred+warn / default google back-compat),
  * google-family requests are byte-for-byte identical to before (compatibility
    contract),
  * microsoft mail_search executes the Outlook slug with query-compiler-mapped
    args, and normalizes Graph-shaped payloads to the canonical schemas.py shapes,
  * a tool_slugs config override wins over the family default,
  * an unknown-tool error names the slug AND the tool_slugs override path,
  * capabilities honesty for the composio_microsoft provider name,
  * connect flow accepts outlook/one_drive,
  * bootstrap --composio-family microsoft overlay,
  * a verify smoke pass with a fake microsoft-family client.

All MCP calls are mocked — no network.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
for p in (SHARED_SCRIPTS, SHARED_SCRIPTS / "providers", PLUGIN_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import schemas  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────

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
    return {"integrations": {"workspace": ws}, "paths": {"project_root": "/tmp/test-ms"}}


def _google_workspace(**extra):
    ws = {
        "provider": "composio",
        "mode": "mcp",
        "user_id": "test-user",
        "toolkits": ["gmail", "googlecalendar", "googledrive"],
        "mcp": {"endpoint": "https://connect.composio.dev/mcp", "key_env": "COMPOSIO_MCP_KEY"},
    }
    ws.update(extra)
    return {"integrations": {"workspace": ws}, "paths": {"project_root": "/tmp/test-g"}}


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


def _ok(data):
    """A successful COMPOSIO_MULTI_EXECUTE_TOOL response wrapping ``data``."""
    return {"data": {"results": [{"response": {"successful": True, "data": data}}]}}


def _err(error):
    return {"data": {"results": [{"response": {"successful": False, "error": error}}]}}


# Graph-shaped payloads (as Composio surfaces Outlook/OneDrive) ────────────────
GRAPH_MESSAGE = {
    "id": "AAMkAG=",
    "conversationId": "conv1",
    "subject": "Invoice due",
    "from": {"emailAddress": {"address": "billing@acme.com", "name": "Acme"}},
    "receivedDateTime": "2026-07-09T08:15:00Z",
    "bodyPreview": "Please pay...",
    "categories": ["AR"],
    "hasAttachments": True,
    "webLink": "https://outlook.office365.com/x",
}
GRAPH_EVENT = {
    "id": "evt1",
    "subject": "Board sync",
    "start": {"dateTime": "2026-07-10T09:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-07-10T10:00:00.0000000", "timeZone": "UTC"},
    "attendees": [{"emailAddress": {"address": "a@x.com"}}],
    "organizer": {"emailAddress": {"address": "b@x.com"}},
    "location": {"displayName": "Teams"},
    "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/j"},
    "showAs": "busy",
}
GRAPH_FILE = {
    "id": "f1",
    "name": "NDA.docx",
    "file": {"mimeType": "application/vnd.openxmlformats"},
    "lastModifiedDateTime": "2026-07-01T00:00:00Z",
    "webUrl": "https://onedrive/x",
    "parentReference": {"id": "p1"},
}


# ── family selection ─────────────────────────────────────────────────────────

class TestFamilySelection:
    def test_explicit_microsoft(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        assert client.family == "microsoft"
        assert client.provider_name == "composio_microsoft:mcp"

    def test_explicit_google(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_google_workspace(family="google"))
        assert client.family == "google"
        assert client.provider_name == "composio:mcp"

    def test_default_is_google(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        # No family key, google toolkits -> google, provider name unchanged.
        client = ComposioMCPWorkspaceClient(_google_workspace())
        assert client.family == "google"
        assert client.provider_name == "composio:mcp"

    def test_inferred_microsoft_warns(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        cfg = _google_workspace()  # google shape...
        cfg["integrations"]["workspace"]["toolkits"] = ["outlook", "one_drive"]
        cfg["integrations"]["workspace"].pop("family", None)
        with pytest.warns(UserWarning, match="inferring family='microsoft'"):
            client = ComposioMCPWorkspaceClient(cfg)
        assert client.family == "microsoft"

    def test_unknown_family_defaults_google_with_warning(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        with pytest.warns(UserWarning, match="not one of"):
            client = ComposioMCPWorkspaceClient(_google_workspace(family="office365"))
        assert client.family == "google"

    def test_family_toolkit_mismatch_microsoft_with_gmail_warns(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        cfg = _ms_workspace()
        cfg["integrations"]["workspace"]["toolkits"] = ["gmail", "googlecalendar"]
        with pytest.warns(UserWarning, match="family='microsoft'.*gmail"):
            client = ComposioMCPWorkspaceClient(cfg)
        assert client.family == "microsoft"

    def test_family_toolkit_mismatch_google_with_outlook_warns(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        cfg = _google_workspace(family="google")
        cfg["integrations"]["workspace"]["toolkits"] = ["outlook", "one_drive"]
        with pytest.warns(UserWarning, match="family='google'.*outlook"):
            client = ComposioMCPWorkspaceClient(cfg)
        assert client.family == "google"


class TestResolveComposioFamilyShared:
    def test_explicit_and_inferred(self):
        from composio_family import _resolve_composio_family
        assert _resolve_composio_family({"family": "microsoft"}) == "microsoft"
        assert _resolve_composio_family({"family": "google"}) == "google"
        assert _resolve_composio_family({}) == "google"
        with pytest.warns(UserWarning, match="inferring family='microsoft'"):
            assert _resolve_composio_family(
                {"toolkits": ["outlook", "one_drive"]}
            ) == "microsoft"
        with pytest.warns(UserWarning, match="not one of"):
            assert _resolve_composio_family({"family": "office365"}) == "google"

    def test_connect_workspace_uses_shared_helper(self):
        import connect_workspace as cw
        from composio_family import _resolve_composio_family
        ws = {"family": "microsoft", "toolkits": ["outlook"]}
        assert cw._composio_family(ws) == _resolve_composio_family(ws, warn=False)
        with pytest.warns(UserWarning, match="inferring"):
            assert cw._composio_family({"toolkits": ["one_drive"]}) == "microsoft"

class TestGoogleFamilyByteForByte:
    def _client(self, family=None):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        cfg = _google_workspace() if family is None else _google_workspace(family=family)
        return ComposioMCPWorkspaceClient(cfg)

    def test_mail_search_uses_gmail_slug_and_args(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"messages": [{"id": "m1", "subject": "T"}]})
        client._mcp_client = mock
        result = client.mail_search("is:unread", max_results=5)
        assert len(result) == 1 and result[0]["subject"] == "T"  # raw passthrough, NOT normalized
        tools = mock.call_tool.call_args[0][1]["tools"]
        assert tools[0]["tool_slug"] == "GMAIL_FETCH_EMAILS"
        # Lean args are a contract (field briefing 2026-08-29): with Composio's
        # default include_payload=true/verbose=true, larger result sets get the
        # body offloaded to data_preview (data=None), which _validate_read_payload
        # rejects as malformed. Briefing/triage needs metadata + snippets only.
        assert tools[0]["arguments"] == {
            "query": "is:unread",
            "max_results": 5,
            "include_payload": False,
            "verbose": False,
        }

    def test_calendar_create_uses_google_args(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "evt"})
        client._mcp_client = mock
        client.calendar_create("Sync", "2026-07-10", "2026-07-10")
        args = mock.call_tool.call_args[0][1]["tools"][0]["arguments"]
        assert args["summary"] == "Sync"  # google uses summary/start_datetime
        assert "start_datetime" in args

    def test_files_upload_uses_drive_slug(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "file"})
        client._mcp_client = mock
        # GOOGLEDRIVE_UPLOAD_FILE needs a staged file_to_upload — patch the MCP
        # sandbox stager (PR #14, no COMPOSIO_API_KEY).
        with patch(
            "composio_files.stage_file_uploadable_via_sandbox",
            return_value={"name": "x.pdf", "mimetype": "application/pdf", "s3key": "k"},
        ):
            client.files_upload("/tmp/x.pdf", parent_id="folder")
        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "GOOGLEDRIVE_UPLOAD_FILE"
        assert call["arguments"]["file_to_upload"]["s3key"] == "k"


# ── microsoft mail_search: slug + query-compiled args ────────────────────────

class TestMicrosoftMailSearch:
    def _client(self, **extra):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(_ms_workspace(**extra))

    def test_mail_search_executes_outlook_slug_with_mapped_args(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"value": [GRAPH_MESSAGE]})
        client._mcp_client = mock
        result = client.mail_search("in:inbox is:unread", max_results=7)
        tools = mock.call_tool.call_args[0][1]["tools"]
        # Live-verified slug (OUTLOOK_QUERY_EMAILS): folder-scoped OData query.
        assert tools[0]["tool_slug"] == "OUTLOOK_QUERY_EMAILS"
        args = tools[0]["arguments"]
        # compile_query('in:inbox is:unread','m365') -> folder inbox + isRead filter
        assert args.get("folder") == "inbox"
        assert "isRead eq false" in (args.get("filter") or "")
        assert args.get("top") == 7
        # 'max_results' is NOT a real OUTLOOK_QUERY_EMAILS argument — 'top' is.
        assert "max_results" not in args
        # Records are normalized to the canonical schema shape.
        assert len(result) == 1
        assert result[0]["sender"] == "billing@acme.com"
        assert result[0]["source"] == "outlook"
        schemas.validate_message(result[0])

    def test_slug_override_wins(self, mcp_key, tmp_project):
        client = self._client(tool_slugs={"mail_search": "OUTLOOK_CUSTOM_SEARCH"})
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"value": []})
        client._mcp_client = mock
        client.mail_search("is:unread")
        assert mock.call_tool.call_args[0][1]["tools"][0]["tool_slug"] == "OUTLOOK_CUSTOM_SEARCH"

    def test_raw_passthrough_via_dict_model(self, mcp_key, tmp_project):
        # OUTLOOK_QUERY_EMAILS ignores search — raw search is dropped with a
        # warning; folder is still applied.
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"value": []})
        client._mcp_client = mock
        query = {"raw": {"m365": {"folder": "sentitems", "filter": None,
                                  "search": "subject:contract"}}}
        with pytest.warns(UserWarning, match="does not support text search"):
            client.mail_search(query)
        args = mock.call_tool.call_args[0][1]["tools"][0]["arguments"]
        assert args["folder"] == "sentitems"
        assert "search" not in args

    def test_raw_passthrough_keeps_search_when_slug_supports_kql(self, mcp_key, tmp_project):
        client = self._client(tool_slugs={"mail_search": "OUTLOOK_KQL_SEARCH"})
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"value": []})
        client._mcp_client = mock
        query = {"raw": {"m365": {"folder": "sentitems", "filter": None,
                                  "search": "subject:contract"}}}
        client.mail_search(query)
        args = mock.call_tool.call_args[0][1]["tools"][0]["arguments"]
        assert args["folder"] == "sentitems"
        assert args["search"] == "subject:contract"

    def test_compile_failure_warns_and_broadens_to_top_only(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"value": []})
        client._mcp_client = mock
        with patch(
            "providers.composio_mcp_workspace.compile_query",
            side_effect=ValueError("untranslatable"),
        ):
            with pytest.warns(UserWarning, match="broadening to list recent mail"):
                client.mail_search("in:anywhere", max_results=3)
        args = mock.call_tool.call_args[0][1]["tools"][0]["arguments"]
        assert args == {"top": 3}


class TestMicrosoftWriteArgs:
    def _client(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(_ms_workspace())

    def test_microsoft_draft_args_match_composio_catalog(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "draft"})
        client._mcp_client = mock

        res = client.mail_create_draft("a@b.com", "S", "B", cc="c@d.com")

        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "OUTLOOK_CREATE_DRAFT"
        args = call["arguments"]
        assert args["to_recipients"] == ["a@b.com"]
        assert args["cc_recipients"] == ["c@d.com"]
        assert args["body"] == "B"
        assert args["is_html"] is True
        assert "toRecipients" not in args
        assert res["tool_slug"] == "OUTLOOK_CREATE_DRAFT"
        assert res["data"]["id"] == "draft"

    def test_microsoft_calendar_create_args_match_composio_catalog(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "evt"})
        client._mcp_client = mock

        client.calendar_create(
            "Sync", "2026-07-10", "2026-07-10",
            attendees=["a@x.com"], description="Discuss",
        )

        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "OUTLOOK_CALENDAR_CREATE_EVENT"
        args = call["arguments"]
        assert args["subject"] == "Sync"
        assert args["start_datetime"] == "2026-07-10T10:00:00"
        assert args["end_datetime"] == "2026-07-10T11:00:00"
        assert args["time_zone"] == "UTC"
        assert args["attendees_info"] == ["a@x.com"]
        assert args["body"] == "Discuss"
        assert args["is_html"] is True
        assert "start" not in args

    def test_microsoft_calendar_update_args_match_composio_catalog(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "evt"})
        client._mcp_client = mock

        client.calendar_update("e1", title="New", start="2026-07-10", description="Updated")

        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "OUTLOOK_UPDATE_CALENDAR_EVENT"
        args = call["arguments"]
        assert args["event_id"] == "e1"
        assert args["subject"] == "New"
        assert args["start_datetime"] == "2026-07-10T10:00:00"
        assert args["time_zone"] == "UTC"
        assert args["body"] == {"contentType": "HTML", "content": "Updated"}
        assert "title" not in args
        assert "start" not in args

    def test_microsoft_files_upload_stages_fileuploadable(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "file"})
        client._mcp_client = mock
        staged = {
            "name": "x.pdf",
            "mimetype": "application/pdf",
            "s3key": "uploads/abc/x.pdf",
        }

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(b"%PDF-1.4")
            path = fh.name
        try:
            with patch.object(
                client, "_ms_stage_file_uploadable", return_value=staged,
            ) as stage:
                client.files_upload(path, parent_id="folder")
            stage.assert_called_once_with(path, "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE")
        finally:
            Path(path).unlink(missing_ok=True)

        call = mock.call_tool.call_args[0][1]["tools"][0]
        assert call["tool_slug"] == "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE"
        args = call["arguments"]
        assert args == {"file": staged, "folder": "folder"}
        assert "file_path" not in args
        assert "parent_id" not in args
        assert "parentReference" not in args

    def test_audit_slug_matches_family_microsoft(self, mcp_key, tmp_project):
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"id": "draft"})
        client._mcp_client = mock

        with patch("workspace_audit.audit_workspace_action") as audit:
            res = client.mail_create_draft("a@b.com", "S", "B")

        assert res["success"] is True
        assert res["tool_slug"] == "OUTLOOK_CREATE_DRAFT"
        assert audit.call_args[0][3] == "OUTLOOK_CREATE_DRAFT"


class TestUnknownToolError:
    def test_read_propagates_unknown_tool_error(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import (
            ComposioMCPWorkspaceClient, ComposioToolError,
        )
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _err("Tool not found: OUTLOOK_QUERY_EMAILS")
        client._mcp_client = mock
        with pytest.raises(ComposioToolError) as ei:
            client.mail_search("is:unread")
        text = str(ei.value)
        assert "OUTLOOK_QUERY_EMAILS" in text
        assert "tool_slugs" in text
        assert "mail_search" in text

    def test_write_surfaces_slug_and_override_path(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _err("unknown tool OUTLOOK_CREATE_DRAFT")
        client._mcp_client = mock
        res = client.mail_create_draft("a@b.com", "S", "B")
        assert res["success"] is False
        assert "OUTLOOK_CREATE_DRAFT" in res["error"]
        assert "tool_slugs" in res["error"]

    def test_non_unknown_error_is_not_enriched(self, mcp_key, tmp_project):
        # Soft failures (rate limits, etc.) must raise ComposioReadError — never
        # silent [] — and must NOT be enriched with the unknown-tool /
        # tool_slugs override path.
        from providers.composio_mcp_workspace import (
            ComposioMCPWorkspaceClient, ComposioReadError,
        )
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _err("rate limited, try again")
        client._mcp_client = mock
        with pytest.raises(ComposioReadError) as ei:
            client.mail_search("is:unread")
        text = str(ei.value)
        assert "rate limited" in text.lower()
        assert "tool_slugs" not in text
        assert ei.value.operation == "mail_search"

    def test_rate_limit_error_raises_composio_read_error(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import (
            ComposioMCPWorkspaceClient, ComposioReadError,
        )
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _err("rate limited, try again later")
        client._mcp_client = mock
        with pytest.raises(ComposioReadError) as ei:
            client.calendar_list("2026-07-01", "2026-07-02")
        assert ei.value.operation == "calendar_list"
        assert "rate limited" in str(ei.value).lower()


class TestErrorClassifierOrdering:
    """P0-2: connection check before unknown-tool; no bare 'not found' needle."""

    def test_connection_not_found_classified_as_connection_error(self):
        from providers.composio_mcp_workspace import (
            _is_connection_error, _is_unknown_tool_error,
        )
        err = "connection not found for toolkit outlook"
        assert _is_connection_error(err) is True
        assert _is_unknown_tool_error(err) is False

    def test_message_not_found_not_classified_as_unknown_tool(self):
        from providers.composio_mcp_workspace import _is_unknown_tool_error
        assert _is_unknown_tool_error("Message not found") is False

    def test_user_not_found_not_classified_as_unknown_tool(self):
        from providers.composio_mcp_workspace import _is_unknown_tool_error
        assert _is_unknown_tool_error("User not found") is False

    def test_tool_not_found_still_unknown_tool(self):
        from providers.composio_mcp_workspace import _is_unknown_tool_error
        assert _is_unknown_tool_error("Tool not found: OUTLOOK_QUERY_EMAILS") is True

    def test_connection_not_found_raises_connection_error(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import (
            ComposioMCPWorkspaceClient, ComposioConnectionError,
        )
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _err("connection not found for toolkit outlook")
        client._mcp_client = mock
        with pytest.raises(ComposioConnectionError):
            client._execute_composio_tool(
                "OUTLOOK_QUERY_EMAILS", {"top": 1}, operation="mail_search"
            )


class TestNoActiveConnection:
    """A no-active-connection failure arrives as a BATCH-level envelope
    (results[0]['error'] with NO 'response' wrapper) — the real shape observed
    live when one_drive is not connected. It must be surfaced, not swallowed
    into an empty successful read (which would falsely certify files_read)."""

    @staticmethod
    def _conn_err(toolkit):
        return {
            "data": {
                "results": [{
                    "error": (f"No active connection found for toolkit(s) "
                              f"'{toolkit}' in this session. To fix this, call "
                              f"COMPOSIO_MANAGE_CONNECTIONS with toolkits=['{toolkit}']"
                              f" to establish a connection, then retry this tool call."),
                    "tool_slug": "ONE_DRIVE_SEARCH_ITEMS",
                    "index": 0,
                }],
                "total_count": 1, "success_count": 0, "error_count": 1,
            },
            "error": "1 out of 1 tools failed",
            "successful": False,
        }

    def test_read_propagates_connection_error(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import (
            ComposioMCPWorkspaceClient, ComposioConnectionError,
        )
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = self._conn_err("one_drive")
        client._mcp_client = mock
        with pytest.raises(ComposioConnectionError) as ei:
            client.files_search("a")
        text = str(ei.value).lower()
        assert "no active connection" in text
        assert "one_drive" in text

    def test_execute_raises_connection_error(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import (
            ComposioMCPWorkspaceClient, ComposioConnectionError,
        )
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = self._conn_err("one_drive")
        client._mcp_client = mock
        with pytest.raises(ComposioConnectionError):
            client._execute_composio_tool(
                "ONE_DRIVE_SEARCH_ITEMS", {"q": "a", "top": 1}, operation="files_search"
            )


# ── microsoft normalizers conform to schemas.py ──────────────────────────────

class TestMicrosoftNormalizers:
    def test_message_conforms(self):
        from providers.composio_mcp_workspace import _ms_normalize_message
        rec = _ms_normalize_message(GRAPH_MESSAGE)
        schemas.validate_message(rec)
        assert rec["source"] == "outlook"
        assert rec["tags"] == ["AR"]
        assert rec["thread_id"] == "conv1"

    def test_event_conforms(self):
        from providers.composio_mcp_workspace import _ms_normalize_event
        rec = _ms_normalize_event(GRAPH_EVENT)
        schemas.validate_event(rec)
        assert rec["conference_link"] == "https://teams.microsoft.com/j"
        assert rec["attendees"] == ["a@x.com"]

    def test_file_conforms(self):
        from providers.composio_mcp_workspace import _ms_normalize_file
        rec = _ms_normalize_file(GRAPH_FILE)
        schemas.validate_file(rec)
        assert rec["source"] == "onedrive"
        assert rec["parents"] == ["p1"]

    def test_sparse_records_conform(self):
        from providers.composio_mcp_workspace import (
            _ms_normalize_message, _ms_normalize_event, _ms_normalize_file,
        )
        msg = _ms_normalize_message({"id": "m1", "receivedDateTime": "2026-07-09T00:00:00Z"})
        schemas.validate_message(msg)
        assert msg["sender"] == "unknown" and msg["subject"] == "(no subject)"
        evt = _ms_normalize_event({
            "id": "e1", "subject": "t",
            "start": {"dateTime": "2026-07-10T09:00:00"},
            "end": {"dateTime": "2026-07-10T10:00:00"},
        })
        schemas.validate_event(evt)
        fil = _ms_normalize_file({"id": "f1", "name": "n"})
        schemas.validate_file(fil)

    def test_envelope_of_normalized_records_conforms(self):
        from providers.composio_mcp_workspace import (
            _ms_normalize_message, _ms_normalize_event, _ms_normalize_file,
        )
        payload = {
            "source": "outlook",
            "messages": [_ms_normalize_message(GRAPH_MESSAGE)],
            "events": [_ms_normalize_event(GRAPH_EVENT)],
            "files": [_ms_normalize_file(GRAPH_FILE)],
        }
        schemas.validate_workspace_payload(payload)

    def test_calendar_and_files_reads_normalize(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"value": [GRAPH_EVENT]})
        client._mcp_client = mock
        events = client.calendar_list("2026-07-10", "2026-07-11")
        assert events[0]["source"] == "outlook"
        schemas.validate_event(events[0])
        cal_call = mock.call_tool.call_args[0][1]["tools"][0]
        # Live-verified slug + snake_case ISO datetime args (NOT camelCase).
        assert cal_call["tool_slug"] == "OUTLOOK_GET_CALENDAR_VIEW"
        assert cal_call["arguments"].get("start_datetime")
        assert cal_call["arguments"].get("end_datetime")
        assert "startDateTime" not in cal_call["arguments"]

        mock.call_tool.return_value = _ok({"value": [GRAPH_FILE]})
        files = client.files_search("NDA")
        assert files[0]["source"] == "onedrive"
        schemas.validate_file(files[0])
        files_call = mock.call_tool.call_args[0][1]["tools"][0]
        # Live-catalog slug (ONE_DRIVE_SEARCH_ITEMS) requires 'q', not 'query'.
        assert files_call["tool_slug"] == "ONE_DRIVE_SEARCH_ITEMS"
        assert files_call["arguments"].get("q") == "NDA"
        assert "query" not in files_call["arguments"]


# ── capabilities honesty ─────────────────────────────────────────────────────

class TestCapabilities:
    # v0.3.12: Phase 4 tags + OneDrive text upload (CREATE_TEXT_FILE) True;
    # calendar.cancel still False.
    def test_composio_microsoft_supported_set(self):
        from workspace_capabilities import get_capabilities
        caps = get_capabilities("composio_microsoft:mcp")
        assert caps["mail.search"] is True
        assert caps["mail.draft"] is True
        assert caps["mail.send"] is True
        assert caps["mail.list_folders"] is True
        assert caps["mail.move"] is True
        assert caps["mail.list_tags"] is True
        assert caps["mail.tag"] is True
        assert caps["mail.create_tag"] is True
        assert caps["calendar.list"] is True
        assert caps["calendar.create"] is True
        assert caps["calendar.update"] is True
        assert caps["calendar.delete"] is True
        assert caps["mail.trash"] is True
        assert caps["mail.archive"] is True
        assert caps["mail.unarchive"] is True
        assert caps["mail.untrash"] is True
        assert caps["gmail.draft"] is True
        assert caps["gmail.trash"] is True
        # files.upload True (PR #14): binary via MCP sandbox staging (no key).
        assert caps["files.upload"] is True
        assert caps["files.download"] is True
        assert caps["files.trash"] is True
        assert caps["drive.trash"] is True

    def test_composio_microsoft_false_ops_have_reasons(self):
        from workspace_capabilities import get_capabilities, get_unsupported_reason
        caps = get_capabilities("composio_microsoft:mcp")
        assert caps["mail.send"] is True
        assert caps["mail.tag"] is True
        assert caps["calendar.cancel"] is False
        assert caps["mail.trash"] is True
        assert caps["mail.draft"] is True
        assert caps["mail.archive"] is True
        # files.upload is supported (PR #14) — no UNSUPPORTED_REASONS entry.
        assert caps["files.upload"] is True
        assert ("composio_microsoft:mcp", "files.upload") not in __import__(
            "workspace_capabilities"
        ).UNSUPPORTED_REASONS

    def test_client_capabilities_use_microsoft_entry(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        assert client.supports("mail.search") is True
        assert client.supports("mail.send") is True
        assert client.supports("mail.draft") is True
        assert client.supports("mail.archive") is True
        assert client.supports("mail.list_folders") is True
        assert client.supports("mail.tag") is True
        assert client.supports("files.upload") is True   # MCP sandbox staging (PR #14)


# ── connect flow accepts outlook / one_drive ─────────────────────────────────

class TestConnectFlow:
    def test_connect_accepts_outlook_and_one_drive(self, mcp_key, tmp_project):
        import connect_workspace as cw
        cfg = _ms_workspace()
        for tk in ("outlook", "one_drive", "share_point"):
            mock_client = MagicMock()
            mock_client.endpoint = "https://connect.composio.dev/mcp"
            mock_client._manage_connections.return_value = {
                "results": {tk: {"redirect_url": f"https://composio.dev/connect/{tk}", "accounts": []}}
            }
            from unittest.mock import patch
            with patch("providers.composio_mcp_workspace.ComposioMCPWorkspaceClient",
                       return_value=mock_client):
                rc = cw.cmd_composio_connect(cfg, tk)
            assert rc == 0
            mock_client._manage_connections.assert_called_once_with("connect", tk)

    def test_provider_info_lists_microsoft_connect_commands(self, mcp_key):
        import io
        from contextlib import redirect_stdout
        import connect_workspace as cw
        buf = io.StringIO()
        with redirect_stdout(buf):
            cw.cmd_provider_composio(_ms_workspace(), print_steps=True)
        out = buf.getvalue()
        assert "--connect outlook" in out
        assert "--connect one_drive" in out
        assert "--connect share_point" in out
        assert "family: microsoft" in out

    def test_capabilities_command_shows_microsoft_provider(self):
        import io
        from contextlib import redirect_stdout
        from connect_workspace import cmd_capabilities
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_capabilities(_ms_workspace(), provider_override="composio")
        out = buf.getvalue()
        assert rc == 0
        assert "composio_microsoft:mcp" in out
        assert "mail.send" in out
        assert "destructive" in out
        assert "intentionally disabled" not in out.split("mail.send", 1)[1][:200]

    def test_test_help_lists_microsoft_toolkits(self):
        import connect_workspace as cw
        help_text = cw.build_parser().format_help()
        assert "outlook" in help_text
        assert "one_drive" in help_text
        assert "share_point" in help_text

    def test_debug_tool_accepts_microsoft_toolkits(self):
        import io
        from contextlib import redirect_stdout
        import connect_workspace as cw

        for tk in ("outlook", "one_drive", "onedrive", "outlook_calendar", "share_point"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cw.cmd_composio_debug_tool(_ms_workspace(), tk)
            assert rc == 1
            assert "Unknown toolkit" not in buf.getvalue()

    def test_test_command_accepts_outlook_calendar_alias(self, monkeypatch):
        import connect_workspace as cw
        mock_client = MagicMock()
        mock_client.calendar_list.return_value = [{"id": "event-1"}]

        monkeypatch.setattr("workspace_client.get_workspace_client", lambda config: mock_client)
        rc = cw.cmd_composio_test(_ms_workspace(), "outlook_calendar")

        assert rc == 0
        mock_client.calendar_list.assert_called_once()


# ── bootstrap --composio-family microsoft ────────────────────────────────────

def _boot_args(**overrides):
    base = dict(
        company=None, jurisdiction=None, operator=None, operator_name=None,
        assistant_name="Chief of Staff", project_root=None, business_type=None,
        config=None, json=False, workspace_provider=None,
        m365_auth="client_credentials", tenant_id=None, client_id=None,
        user_principal=None, m365_secret_env="M365_CLIENT_SECRET",
        composio_user_id=None, composio_family="google",
        esign_url=None, allow_insecure_esign_url=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestBootstrapMicrosoftFamily:
    def test_microsoft_overlay(self):
        import bootstrap
        overlay, req, notices, nxt = bootstrap._provider_overlay(_boot_args(
            workspace_provider="composio", composio_user_id="acme-alicia",
            composio_family="microsoft"))
        ws = overlay["integrations"]["workspace"]
        assert ws["provider"] == "composio"
        assert ws["family"] == "microsoft"
        assert ws["toolkits"] == ["outlook", "one_drive", "share_point"]
        assert ws["user_id"] == "acme-alicia"
        assert req == ["COMPOSIO_MCP_KEY"]
        joined = " ".join(nxt)
        assert "--connect outlook" in joined
        assert "--connect one_drive" in joined
        assert "--connect share_point" in joined
        # No secret value written anywhere in the overlay.
        assert "test-key" not in str(overlay)

    def test_default_composio_family_is_google(self):
        import bootstrap
        overlay, *_ = bootstrap._provider_overlay(_boot_args(
            workspace_provider="composio", composio_user_id="x"))
        ws = overlay["integrations"]["workspace"]
        assert ws["family"] == "google"
        assert ws["toolkits"] == ["gmail", "googlecalendar", "googledrive"]

    def test_main_microsoft_prints_connect_commands(self, tmp_path, monkeypatch, capsys):
        import shutil
        import bootstrap
        cfg = tmp_path / "config"
        cfg.mkdir()
        shutil.copy2(PLUGIN_ROOT / "shared" / "config" / "company.yaml.example",
                     cfg / "company.yaml.example")
        monkeypatch.setattr(bootstrap, "CONFIG_DIR", cfg)
        monkeypatch.setattr(bootstrap, "run_checks", lambda *a, **k: [])
        rc = bootstrap._main([
            "--workspace-provider", "composio",
            "--composio-family", "microsoft",
            "--composio-user-id", "acme-alicia",
            "--project-root", str(tmp_path / "proj"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "--connect outlook" in out
        assert "--connect one_drive" in out
        assert "--connect share_point" in out


# ── verify smoke with a fake microsoft-family client ─────────────────────────

class TestVerifySmoke:
    def test_verify_read_ready_with_fake_microsoft_client(self, monkeypatch):
        import workspace_verify

        class FakeMSClient:
            provider_name = "composio_microsoft:mcp"

            def health_check(self):
                return True

            def mail_search(self, query, max_results=10):
                return [{"id": "m1"}]

            def mail_list_tags(self):
                raise NotImplementedError("composio microsoft has no tags")

            def calendar_list(self, start, end):
                return [{"id": "e1"}]

            def files_search(self, query, max_results=10):
                return [{"id": "f1"}]

            def supports(self, action):
                from workspace_capabilities import get_capabilities
                return get_capabilities("composio_microsoft:mcp").get(action, False)

        monkeypatch.setattr(workspace_verify, "get_workspace_client",
                            lambda cfg: FakeMSClient())
        report = workspace_verify.run_verification(_ms_workspace(), include_writes=False)
        assert report["provider"] == "composio_microsoft:mcp"
        assert report["read_ready"] is True
        # mail_tags_list is OPTIONAL: unsupported doesn't block read_ready.
        assert report["checks"]["mail_tags_list"]["status"] == "fail"

    def test_soft_error_fails_verification(self, monkeypatch, mcp_key, tmp_project):
        """successful: False must not certify read_ready (P0-1 regression)."""
        import warnings

        import workspace_verify
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _err("rate limited, try again")
        client._mcp_client = mock

        monkeypatch.setattr(
            workspace_verify, "get_workspace_client", lambda cfg: client
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            report = workspace_verify.run_verification(
                _ms_workspace(), include_writes=False
            )
        assert report["read_ready"] is False
        assert report["checks"]["mail_read"]["status"] == "fail"
