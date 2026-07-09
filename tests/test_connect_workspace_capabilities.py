#!/usr/bin/env python3
"""Tests for connect_workspace.py --capabilities and workspace_capabilities workflow helpers."""

import sys
import io
import os
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


class TestWorkspaceCapabilitiesExtended:
    """Test workflow requirements, unsupported reasons, and provider recommendations."""

    def test_google_api_gmail_draft_is_false(self):
        from workspace_capabilities import get_capabilities
        caps = get_capabilities("google_api")
        assert caps["gmail.draft"] is False

    def test_composio_mcp_gmail_draft_is_true(self):
        from workspace_capabilities import get_capabilities
        caps = get_capabilities("composio:mcp")
        assert caps["gmail.draft"] is True

    def test_unsupported_reason_google_api_draft(self):
        from workspace_capabilities import get_unsupported_reason
        reason = get_unsupported_reason("google_api", "gmail.draft")
        assert "google_api.py" in reason
        assert "draft subcommand" in reason

    def test_unsupported_reason_composio_send(self):
        from workspace_capabilities import get_unsupported_reason
        reason = get_unsupported_reason("composio:mcp", "gmail.send")
        assert "intentionally disabled" in reason

    def test_recommend_provider_for_draft(self):
        from workspace_capabilities import recommend_provider_for
        assert recommend_provider_for("gmail.draft") == "composio"

    def test_recommend_provider_for_handoff(self):
        from workspace_capabilities import recommend_provider_for
        assert recommend_provider_for("document.handoff") == "composio"

    def test_recommend_provider_for_calendar_create(self):
        from workspace_capabilities import recommend_provider_for
        assert "google_api" in recommend_provider_for("calendar.create")

    def test_workflow_supported_handoff_google_api(self):
        """document.handoff should fail under google_api (missing gmail.draft)."""
        from workspace_capabilities import workflow_supported
        mock_client = MagicMock()
        mock_client.supports.side_effect = lambda action: action != "gmail.draft"
        ok, missing = workflow_supported(mock_client, "document.handoff")
        assert ok is False
        assert "gmail.draft" in missing

    def test_workflow_supported_handoff_composio(self):
        """document.handoff should pass under composio:mcp."""
        from workspace_capabilities import workflow_supported
        mock_client = MagicMock()
        mock_client.supports.side_effect = lambda action: True
        ok, missing = workflow_supported(mock_client, "document.handoff")
        assert ok is True
        assert missing == []

    def test_workflow_supported_meeting_gather(self):
        """meeting.gather should pass under both providers (all reads supported)."""
        from workspace_capabilities import workflow_supported
        mock_google = MagicMock()
        mock_google.supports.side_effect = lambda action: action != "gmail.draft"
        ok, missing = workflow_supported(mock_google, "meeting.gather")
        assert ok is True

    def test_require_capability_includes_reason(self):
        """require_capability error should include specific reason."""
        from workspace_capabilities import require_capability
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda action: action != "gmail.draft"
        result = require_capability(mock_client, "gmail.draft", target="a@b.com")
        assert result is not None
        assert "google_api.py has no draft subcommand" in result["error"]
        assert "composio" in result["error"]


class TestConnectWorkspaceCapabilities:
    """Test --capabilities command output."""

    def test_capabilities_google_api(self):
        from connect_workspace import cmd_capabilities
        config = {"integrations": {"workspace": {"provider": "google_api"}}}
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_capabilities(config, provider_override="google_api")
        out = buf.getvalue()
        assert rc == 0
        assert "google_api" in out
        assert "gmail.search" in out
        assert "gmail.draft" in out
        assert "❌" in out
        assert "google_api.py has no draft subcommand" in out

    def test_capabilities_composio(self):
        from connect_workspace import cmd_capabilities
        config = {"integrations": {"workspace": {"provider": "composio", "mode": "mcp"}}}
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_capabilities(config, provider_override="composio")
        out = buf.getvalue()
        assert rc == 0
        assert "composio:mcp" in out
        assert "gmail.draft" in out
        assert "❌" in out
        assert "gmail.send" in out
        assert "intentionally disabled" in out

    def test_capabilities_shows_workflows(self):
        from connect_workspace import cmd_capabilities
        config = {"integrations": {"workspace": {"provider": "google_api"}}}
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_capabilities(config, provider_override="google_api")
        out = buf.getvalue()
        assert "Workflows:" in out
        assert "document.handoff" in out
        assert "meeting.gather" in out

    def test_capabilities_google_api_handoff_unsupported(self):
        from connect_workspace import cmd_capabilities
        config = {"integrations": {"workspace": {"provider": "google_api"}}}
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_capabilities(config, provider_override="google_api")
        out = buf.getvalue()
        assert "document.handoff" in out
        assert "❌" in out
        assert "gmail.draft" in out  # mentioned as missing

    def test_capabilities_composio_handoff_supported(self):
        from connect_workspace import cmd_capabilities
        config = {"integrations": {"workspace": {"provider": "composio", "mode": "mcp"}}}
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_capabilities(config, provider_override="composio")
        out = buf.getvalue()
        assert "document.handoff" in out
        assert "✅" in out  # should be supported

    def test_capabilities_default_provider_from_config(self):
        """Without --provider override, uses config's provider."""
        from connect_workspace import cmd_capabilities
        config = {"integrations": {"workspace": {"provider": "google_api"}}}
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_capabilities(config, provider_override=None)
        out = buf.getvalue()
        assert "google_api" in out