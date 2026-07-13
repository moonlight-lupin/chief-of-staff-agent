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
from unittest.mock import MagicMock

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


# ── google-family byte-for-byte compatibility ────────────────────────────────

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
        assert tools[0]["arguments"] == {"query": "is:unread", "max_results": 5}

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
        client.files_upload("/tmp/x.pdf", parent_id="folder")
        assert mock.call_tool.call_args[0][1]["tools"][0]["tool_slug"] == "GOOGLEDRIVE_UPLOAD_FILE"


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
        client = self._client()
        mock = MagicMock()
        mock.call_tool.return_value = _ok({"value": []})
        client._mcp_client = mock
        query = {"raw": {"m365": {"folder": "sentitems", "filter": None,
                                  "search": "subject:contract"}}}
        client.mail_search(query)
        args = mock.call_tool.call_args[0][1]["tools"][0]["arguments"]
        assert args["folder"] == "sentitems"
        assert args["search"] == "subject:contract"


class TestUnknownToolError:
    def test_read_warns_with_slug_and_override_path(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _err("Tool not found: OUTLOOK_QUERY_EMAILS")
        client._mcp_client = mock
        with pytest.warns(UserWarning) as record:
            result = client.mail_search("is:unread")
        assert result == []
        text = " ".join(str(w.message) for w in record)
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
        # A generic tool error is NOT treated as unknown-tool: read returns [] with
        # no raise/enrichment (byte-compatible with prior swallow behaviour).
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _err("rate limited, try again")
        client._mcp_client = mock
        # No warning expected (data returns an error dict, normalized to []).
        result = client.mail_search("is:unread")
        assert result == []


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

    def test_read_warns_and_returns_empty_on_missing_connection(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = self._conn_err("one_drive")
        client._mcp_client = mock
        with pytest.warns(UserWarning) as record:
            result = client.files_search("a")
        assert result == []
        text = " ".join(str(w.message) for w in record)
        assert "no active connection" in text.lower()
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
    def test_composio_microsoft_supported_set(self):
        from workspace_capabilities import get_capabilities
        caps = get_capabilities("composio_microsoft:mcp")
        assert caps["mail.search"] is True
        assert caps["mail.draft"] is True
        assert caps["calendar.list"] is True
        assert caps["files.upload"] is True
        # legacy aliases resolve too
        assert caps["gmail.draft"] is True

    def test_composio_microsoft_false_ops_have_reasons(self):
        from workspace_capabilities import get_capabilities, get_unsupported_reason
        caps = get_capabilities("composio_microsoft:mcp")
        assert caps["mail.send"] is False
        reason = get_unsupported_reason("composio_microsoft:mcp", "mail.send")
        assert "intentionally disabled" in reason
        # archive/trash/tags/cancel are honestly unsupported (no Composio slug).
        for op in ("mail.archive", "mail.trash", "mail.tag", "calendar.cancel", "files.trash"):
            assert caps[op] is False

    def test_client_capabilities_use_microsoft_entry(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        assert client.supports("mail.search") is True
        assert client.supports("mail.send") is False


# ── connect flow accepts outlook / one_drive ─────────────────────────────────

class TestConnectFlow:
    def test_connect_accepts_outlook_and_one_drive(self, mcp_key, tmp_project):
        import connect_workspace as cw
        cfg = _ms_workspace()
        for tk in ("outlook", "one_drive"):
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
        assert "mail.send" in out and "intentionally disabled" in out


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
        assert ws["toolkits"] == ["outlook", "one_drive"]
        assert ws["user_id"] == "acme-alicia"
        assert req == ["COMPOSIO_MCP_KEY"]
        joined = " ".join(nxt)
        assert "--connect outlook" in joined
        assert "--connect one_drive" in joined
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
