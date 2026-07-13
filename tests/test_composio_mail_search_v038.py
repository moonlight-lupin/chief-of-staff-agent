#!/usr/bin/env python3
"""v0.3.8 — Composio Outlook mail-search + hard-read-failure honesty.

P0: OUTLOOK_QUERY_EMAILS ignores ``search``. When a bundled Gmail-syntax query
needs text search (often after the compiler folds filter+search into KQL),
``_ms_mail_search_args`` must prefer any recoverable OData filter and warn that
text search was dropped — not silently emit top=N alone.

P1: ComposioConnectionError / ComposioToolError must propagate from public
reads (not swallow to []), and daily briefing must mark the section unavailable.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
DAILY_BRIEFING = PLUGIN_ROOT / "skills" / "daily-briefing" / "scripts"
for p in (SHARED_SCRIPTS, SHARED_SCRIPTS / "providers", DAILY_BRIEFING, PLUGIN_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


# Bundled templates from shared/config/queries.yaml.example (resolved vars).
BUNDLED_QUERIES = {
    "unread_priority": (
        'in:inbox is:unread newer_than:14d '
        '("urgent" OR "approval" OR "please review" OR "action required" OR "deadline")'
    ),
    "unread_business": "to:delegate@example.com is:unread",
    "engagement_threads": (
        "subject:(proposal OR NDA OR SOW OR invoice OR contract OR engagement "
        'OR "statement of work") newer_than:14d'
    ),
    "acra_iras": (
        "from:(@acra.gov.sg OR @iras.gov.sg OR @bizfile.gov.sg) newer_than:30d"
    ),
}


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
    return {"integrations": {"workspace": ws}, "paths": {"project_root": "/tmp/test-ms-v038"}}


def _ok(data):
    return {"data": {"results": [{"response": {"successful": True, "data": data}}]}}


def _err(error):
    return {"data": {"results": [{"response": {"successful": False, "error": error}}]}}


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


class TestBundledQueryTemplatesDropSearch:
    """Each bundled template that compiles with a search component must warn
    and emit args without ``search`` under the default OUTLOOK_QUERY_EMAILS slug.
    """

    def _client(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        return ComposioMCPWorkspaceClient(_ms_workspace())

    @pytest.mark.parametrize("name", list(BUNDLED_QUERIES))
    def test_bundled_template_drops_search_with_warning(self, name, mcp_key, tmp_project):
        client = self._client()
        query = BUNDLED_QUERIES[name]
        with pytest.warns(UserWarning, match="does not support text search") as record:
            args = client._ms_mail_search_args(query, max_results=10)

        assert "top" in args
        assert args["top"] == 10
        assert "search" not in args

        # Prefer filter when the template has filter-eligible clauses.
        assert "filter" in args, (
            f"{name}: expected recoverable OData filter, got {args!r}"
        )
        warn_text = " ".join(str(w.message) for w in record)
        assert "text search dropped" in warn_text or "does not support text search" in warn_text

        if name == "unread_priority":
            assert args.get("folder") == "inbox"
            assert "isRead eq false" in args["filter"]

    def test_kql_capable_slug_override_keeps_search(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(
            _ms_workspace(tool_slugs={"mail_search": "OUTLOOK_KQL_SEARCH"})
        )
        args = client._ms_mail_search_args(BUNDLED_QUERIES["engagement_threads"], 5)
        assert "search" in args
        assert args["top"] == 5

    def test_explicit_supports_text_search_flag(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(
            _ms_workspace(mail_search_supports_text_search=True)
        )
        args = client._ms_mail_search_args(BUNDLED_QUERIES["acra_iras"], 5)
        assert "search" in args


class TestHardReadFailuresPropagate:
    def test_composio_connection_error_propagates_from_mail_search(
        self, mcp_key, tmp_project,
    ):
        from providers.composio_mcp_workspace import (
            ComposioMCPWorkspaceClient,
            ComposioConnectionError,
            ComposioReadError,
        )
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _err("no active connection for toolkit outlook")
        client._mcp_client = mock
        with pytest.raises(ComposioConnectionError) as ei:
            client.mail_search("is:unread")
        assert isinstance(ei.value, ComposioReadError)
        assert ei.value.operation == "mail_search"
        # Must NOT be swallowed to [].
        assert client.mail_search  # sanity: method still bound

    def test_soft_failure_still_returns_empty(self, mcp_key, tmp_project):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(_ms_workspace())
        mock = MagicMock()
        mock.call_tool.return_value = _err("rate limited, try again")
        client._mcp_client = mock
        with pytest.warns(UserWarning, match="rate limited"):
            assert client.mail_search("is:unread") == []


class TestDailyBriefingUnavailable:
    def test_wrap_source_marks_composio_read_error_unavailable(self, tmp_project):
        import daily_briefing as db
        from providers.composio_mcp_workspace import ComposioConnectionError

        def boom(_config, _root):
            raise ComposioConnectionError(
                "no active connection",
                operation="mail_search",
            )

        src = db.wrap_source("gmail", boom, {}, tmp_project)
        assert src["status"] == "unavailable"
        assert src["items"] == []
        assert "no active connection" in src["error"]

        lines = db.render_source("Gmail", "📧", src, lambda i: str(i))
        assert "unavailable" in lines[0]
        assert "failed" not in lines[0]
