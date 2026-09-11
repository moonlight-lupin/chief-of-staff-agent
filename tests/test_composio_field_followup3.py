#!/usr/bin/env python3
"""Contract tests for CoS field follow-up #3 (2026-09-11).

Written by the orchestrator (Hermes) BEFORE the builder dispatch — these encode
the expected behavior from the live-deployment field note. The builder must make
them pass without modifying them.

Covers:
1. calendar_list: GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS slug, unified events
   normalizer with source_calendar_* passthrough, tz-localized windows from
   delivery.timezone (not hardcoded Z).
2. Multi-account routing: per-tool "account" field in COMPOSIO_MULTI_EXECUTE_TOOL
   from integrations.workspace.account_aliases.<toolkit>.
3. Connection status: COMPOSIO_MANAGE_CONNECTIONS action "list" (not "status")
   + case-insensitive "active" comparison.
4. Status-active helper shared at the adapter boundary.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


def _make_config(aliases: dict | None = None, timezone: str | None = "Asia/Singapore") -> dict:
    """Composio MCP config mirroring tests/test_composio_workspace.py fixtures."""
    workspace: dict = {
        "provider": "composio",
        "mode": "mcp",
        "user_id": "test-user-123",
        "toolkits": ["gmail", "googlecalendar", "googledrive"],
        "mcp": {"endpoint": "https://connect.composio.dev/mcp", "key_env": "COMPOSIO_MCP_KEY"},
        "tools_allowlist": {
            "gmail": {"read": ["GMAIL_FETCH_EMAILS"], "write_safe": ["GMAIL_CREATE_EMAIL_DRAFT"]},
            "googlecalendar": {"read": ["GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS"], "write_safe": ["GOOGLECALENDAR_CREATE_EVENT"]},
            "googledrive": {"read": ["GOOGLEDRIVE_FIND_FILE", "GOOGLEDRIVE_DOWNLOAD_FILE"], "write_safe": ["GOOGLEDRIVE_UPLOAD_FILE"]},
        },
    }
    if aliases is not None:
        workspace["account_aliases"] = aliases
    config = {
        "integrations": {"workspace": workspace},
        "paths": {"project_root": "/tmp/test-composio-f3"},
        "delivery": {"timezone": timezone},
    }
    return config


@pytest.fixture
def mcp_key():
    os.environ["COMPOSIO_MCP_KEY"] = "test-mcp-key"
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
    yield
    os.environ.pop("COMPOSIO_MCP_KEY", None)


def _mock_events_payload():
    """COMPOSIO_MULTI_EXECUTE_TOOL result carrying the unified events-list shape."""
    return {
        "data": {
            "results": [
                {
                    "response": {
                        "successful": True,
                        "data": [
                            {
                                "event": {"id": "e1", "summary": "Standup"},
                                "source_calendar_id": "cal-primary",
                                "source_calendar_summary": "Work",
                            },
                            {
                                "event": {"id": "e2", "summary": "Dentist"},
                                "source_calendar_id": "cal-personal",
                                "source_calendar_summary": "Personal",
                            },
                        ],
                    }
                }
            ]
        }
    }


class TestCalendarListSlugAndWindows:
    """calendar_list uses the unified list endpoint with tz-localized windows."""

    def test_slug_is_events_list_all_calendars(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        client = ComposioMCPWorkspaceClient(_make_config())
        client._mcp_client = MagicMock()
        client._mcp_client.call_tool.return_value = _mock_events_payload()

        client.calendar_list("2026-07-09", "2026-07-10")

        tools_arg = client._mcp_client.call_tool.call_args[0][1]["tools"]
        assert tools_arg[0]["tool_slug"] == "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS"

    def test_windows_localized_from_delivery_timezone(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        client = ComposioMCPWorkspaceClient(_make_config(timezone="Asia/Singapore"))
        client._mcp_client = MagicMock()
        client._mcp_client.call_tool.return_value = _mock_events_payload()

        client.calendar_list("2026-07-09", "2026-07-10")

        args = client._mcp_client.call_tool.call_args[0][1]["tools"][0]["arguments"]
        # 2026-07-09 00:00 SGT == 2026-07-08 16:00 UTC — a bare Z window would
        # read events ending 07:59 SGT the next day. Windows must carry +08:00.
        assert args["time_min"] == "2026-07-09T00:00:00+08:00"
        assert args["time_max"] == "2026-07-10T23:59:59+08:00"
        assert args["max_results_per_calendar"] == 50
        assert args["response_detail"] == "full"
        assert args["single_events"] is True

    def test_windows_fall_back_to_utc_without_delivery_timezone(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        client = ComposioMCPWorkspaceClient(_make_config(timezone=None))
        client._mcp_client = MagicMock()
        client._mcp_client.call_tool.return_value = _mock_events_payload()

        client.calendar_list("2026-07-09", "2026-07-10")

        args = client._mcp_client.call_tool.call_args[0][1]["tools"][0]["arguments"]
        # No configured tz -> UTC windows, still explicit (no bare "Z"-style drift).
        assert args["time_min"] == "2026-07-09T00:00:00+00:00"
        assert args["time_max"] == "2026-07-10T23:59:59+00:00"


class TestEventsNormalizer:
    """Unified events list unwraps {event, source_calendar_*} items."""

    def test_unwraps_event_items_with_source_calendar_passthrough(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        data = [
            {"event": {"id": "e1", "summary": "Standup"},
             "source_calendar_id": "cal-primary", "source_calendar_summary": "Work"},
        ]
        result = ComposioMCPWorkspaceClient._normalize_tool_result(
            "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS", data
        )
        assert isinstance(result, list) and len(result) == 1
        assert result[0]["id"] == "e1"
        assert result[0]["summary"] == "Standup"
        # Multi-calendar reads stay attributable.
        assert result[0]["source_calendar_id"] == "cal-primary"
        assert result[0]["source_calendar_summary"] == "Work"

    def test_calendar_list_returns_flat_event_list(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        client = ComposioMCPWorkspaceClient(_make_config())
        client._mcp_client = MagicMock()
        client._mcp_client.call_tool.return_value = _mock_events_payload()

        result = client.calendar_list("2026-07-09", "2026-07-10")
        assert isinstance(result, list) and len(result) == 2
        assert result[1]["source_calendar_id"] == "cal-personal"


class TestPerToolAccountRouting:
    """account_aliases pin the Composio connection per toolkit."""

    def test_mail_search_routes_to_gmail_alias(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        config = _make_config(aliases={"gmail": "acc-gmail-1", "googlecalendar": "acc-cal-9"})
        client = ComposioMCPWorkspaceClient(config)
        client._mcp_client = MagicMock()
        client._mcp_client.call_tool.return_value = {
            "data": {"results": [{"response": {"successful": True, "data": {"messages": []}}}]}
        }

        client.mail_search("invoice")

        tools_arg = client._mcp_client.call_tool.call_args[0][1]["tools"]
        assert tools_arg[0]["tool_slug"] == "GMAIL_FETCH_EMAILS"
        assert tools_arg[0]["account"] == "acc-gmail-1"

    def test_calendar_list_routes_to_googlecalendar_alias(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        config = _make_config(aliases={"gmail": "acc-gmail-1", "googlecalendar": "acc-cal-9"})
        client = ComposioMCPWorkspaceClient(config)
        client._mcp_client = MagicMock()
        client._mcp_client.call_tool.return_value = _mock_events_payload()

        client.calendar_list("2026-07-09", "2026-07-10")

        tools_arg = client._mcp_client.call_tool.call_args[0][1]["tools"]
        assert tools_arg[0]["account"] == "acc-cal-9"

    def test_no_alias_means_no_account_key(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        client = ComposioMCPWorkspaceClient(_make_config(aliases=None))
        client._mcp_client = MagicMock()
        client._mcp_client.call_tool.return_value = _mock_events_payload()

        client.calendar_list("2026-07-09", "2026-07-10")

        tools_arg = client._mcp_client.call_tool.call_args[0][1]["tools"]
        assert "account" not in tools_arg[0]


class TestConnectionStatus:
    """COMPOSIO_MANAGE_CONNECTIONS uses action "list"; status casing normalized."""

    def _status_mock(self, raw_status: str) -> MagicMock:
        mcp = MagicMock()
        mcp.call_tool.return_value = {
            "data": {
                "results": {
                    "gmail": {"accounts": [{"id": "a1", "status": raw_status}]},
                }
            }
        }
        return mcp

    def test_uses_list_action_not_status(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        client = ComposioMCPWorkspaceClient(_make_config())
        client._mcp_client = self._status_mock("active")

        statuses = client.refresh_connection_statuses()

        call = client._mcp_client.call_tool.call_args[0]
        assert call[0] == "COMPOSIO_MANAGE_CONNECTIONS"
        assert call[1]["action"] == "list"
        assert statuses["gmail"] == "connected"

    def test_mixed_case_active_counts_as_connected(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        client = ComposioMCPWorkspaceClient(_make_config())
        client._mcp_client = self._status_mock("Active")
        assert client.refresh_connection_statuses()["gmail"] == "connected"

        client._mcp_client = self._status_mock(" ACTIVE ")
        assert client.refresh_connection_statuses()["gmail"] == "connected"

    def test_non_active_is_pending(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        client = ComposioMCPWorkspaceClient(_make_config())
        client._mcp_client = self._status_mock("INITIATED")
        assert client.refresh_connection_statuses()["gmail"] == "pending"


class TestStatusIsActiveHelper:
    """One adapter-boundary helper owns the active-status comparison."""

    def test_helper_normalizes_case_and_whitespace(self):
        import providers.composio_mcp_workspace_base as base

        helper = getattr(base, "_status_is_active", None)
        assert helper is not None, "providers.composio_mcp_workspace_base must export _status_is_active"
        assert helper("active") is True
        assert helper("Active") is True
        assert helper(" ACTIVE ") is True
        assert helper("INITIATED") is False
        assert helper(None) is False
        assert helper("") is False


class TestAccountRoutingFamilyGuard:
    """Account routing is family-scoped — Codex review MAJOR fix.

    A toolkit from the OTHER family must never receive an alias, and an
    explicit pin must never be silently dropped when its own family toolkit is
    enabled.
    """

    def _mock(self, payload):
        mcp = MagicMock()
        mcp.call_tool.return_value = payload
        return mcp

    def test_google_family_never_routes_to_outlook_alias(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        config = _make_config(aliases={"gmail": "acc-g", "outlook": "acc-o"})
        # Misconfigured toolkits list contains a foreign-family toolkit.
        config["integrations"]["workspace"]["toolkits"] = ["gmail", "outlook"]
        client = ComposioMCPWorkspaceClient(config)
        client._mcp_client = self._mock({
            "data": {"results": [{"response": {"successful": True, "data": {"messages": []}}}]}
        })

        client.mail_search("invoice")

        tools_arg = client._mcp_client.call_tool.call_args[0][1]["tools"]
        assert tools_arg[0]["tool_slug"] == "GMAIL_FETCH_EMAILS"
        # The outlook alias must NOT ride along on a gmail call.
        assert tools_arg[0]["account"] == "acc-g"

    def test_google_mail_without_gmail_alias_gets_no_account(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        config = _make_config(aliases={"outlook": "acc-o"})
        config["integrations"]["workspace"]["toolkits"] = ["gmail", "outlook"]
        client = ComposioMCPWorkspaceClient(config)
        client._mcp_client = self._mock({
            "data": {"results": [{"response": {"successful": True, "data": {"messages": []}}}]}
        })

        client.mail_search("invoice")

        tools_arg = client._mcp_client.call_tool.call_args[0][1]["tools"]
        assert "account" not in tools_arg[0]

    def test_microsoft_family_routes_to_outlook_alias(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        workspace = {
            "provider": "composio",
            "mode": "mcp",
            "family": "microsoft",
            "user_id": "test-user-123",
            "toolkits": ["outlook", "one_drive"],
            "account_aliases": {"outlook": "acc-outlook-7", "one_drive": "acc-od-2"},
            "mcp": {"endpoint": "https://connect.composio.dev/mcp", "key_env": "COMPOSIO_MCP_KEY"},
        }
        config = {
            "integrations": {"workspace": workspace},
            "paths": {"project_root": "/tmp/test-composio-f3"},
            "delivery": {"timezone": "Asia/Singapore"},
        }
        client = ComposioMCPWorkspaceClient(config)
        client._mcp_client = self._mock({
            "data": {"results": [{"response": {"successful": True, "data": {"value": []}}}]}
        })

        client.mail_search("invoice")

        tools_arg = client._mcp_client.call_tool.call_args[0][1]["tools"]
        assert tools_arg[0]["tool_slug"].startswith("OUTLOOK_")
        assert tools_arg[0]["account"] == "acc-outlook-7"

    def test_alias_for_disabled_toolkit_is_dropped(self, mcp_key):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        # googledrive not in toolkits -> its alias must never be attached.
        config = _make_config(
            aliases={"gmail": "acc-g", "googledrive": "acc-od", "googlecalendar": "acc-cal"},
        )
        config["integrations"]["workspace"]["toolkits"] = ["gmail", "googlecalendar"]
        client = ComposioMCPWorkspaceClient(config)
        client._mcp_client = self._mock(_mock_events_payload())

        client.calendar_list("2026-07-09", "2026-07-10")

        tools_arg = client._mcp_client.call_tool.call_args[0][1]["tools"]
        assert tools_arg[0]["account"] == "acc-cal"


class TestConnectionStatusMalformedEnvelope:
    """Malformed COMPOSIO_MANAGE_CONNECTIONS responses read as unknown."""

    def _client(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient

        client = ComposioMCPWorkspaceClient(_make_config())
        return client

    def test_missing_results_is_unknown_not_pending(self, mcp_key):
        client = self._client()
        mcp = MagicMock()
        mcp.call_tool.return_value = {"data": {"unexpected": True}}
        client._mcp_client = mcp
        assert client.refresh_connection_statuses()["gmail"] == "unknown"

    def test_missing_toolkit_entry_is_unknown_not_pending(self, mcp_key):
        client = self._client()
        mcp = MagicMock()
        mcp.call_tool.return_value = {"data": {"results": {"other_toolkit": {"accounts": []}}}}
        client._mcp_client = mcp
        assert client.refresh_connection_statuses()["gmail"] == "unknown"