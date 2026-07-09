#!/usr/bin/env python3
"""Tests for workspace guardrails — write action confirmation and ActionResult."""

import sys
import os
from pathlib import Path
from unittest.mock import patch

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture(autouse=True)
def clean_env():
    """Remove guardrail env vars before each test."""
    for key in ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"):
        os.environ.pop(key, None)
    yield
    for key in ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"):
        os.environ.pop(key, None)


class TestIsWriteAction:
    def test_gmail_draft_is_write(self):
        from workspace_guardrails import is_write_action
        assert is_write_action("gmail.draft") is True

    def test_calendar_create_is_write(self):
        from workspace_guardrails import is_write_action
        assert is_write_action("calendar.create") is True

    def test_drive_upload_is_write(self):
        from workspace_guardrails import is_write_action
        assert is_write_action("drive.upload") is True

    def test_gmail_search_is_not_write(self):
        from workspace_guardrails import is_write_action
        assert is_write_action("gmail.search") is False

    def test_calendar_list_is_not_write(self):
        from workspace_guardrails import is_write_action
        assert is_write_action("calendar.list") is False


class TestRequiresConfirmation:
    def test_destructive_always_requires(self):
        from workspace_guardrails import requires_confirmation
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        assert requires_confirmation("gmail.send") is True
        assert requires_confirmation("calendar.delete") is True
        assert requires_confirmation("drive.delete") is True

    def test_safe_write_no_confirmation_with_auto_approve(self):
        from workspace_guardrails import requires_confirmation
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        assert requires_confirmation("gmail.draft") is False
        assert requires_confirmation("calendar.create") is False
        assert requires_confirmation("drive.upload") is False

    def test_safe_write_requires_confirmation_without_auto_approve(self):
        from workspace_guardrails import requires_confirmation
        os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)
        assert requires_confirmation("gmail.draft") is True

    def test_read_actions_never_require_confirmation(self):
        from workspace_guardrails import requires_confirmation
        assert requires_confirmation("gmail.search") is False
        assert requires_confirmation("calendar.list") is False
        assert requires_confirmation("drive.search") is False


class TestConfirmAction:
    def test_read_action_always_proceeds(self):
        from workspace_guardrails import confirm_action
        assert confirm_action("gmail.search") is True

    def test_destructive_blocked_without_allow_env(self):
        from workspace_guardrails import confirm_action
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        assert confirm_action("gmail.send") is False

    def test_destructive_allowed_with_allow_env(self):
        from workspace_guardrails import confirm_action
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        os.environ["CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"] = "1"
        assert confirm_action("gmail.send") is True

    def test_safe_write_proceeds_with_auto_approve(self):
        from workspace_guardrails import confirm_action
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        assert confirm_action("gmail.draft", to="test@test.com") is True

    def test_safe_write_blocked_without_auto_approve_non_tty(self):
        from workspace_guardrails import confirm_action
        # In test env, stdin is not a TTY
        assert confirm_action("gmail.draft", to="test@test.com") is False


class TestActionResult:
    def test_success_result(self):
        from workspace_guardrails import ActionResult
        result = ActionResult(
            success=True,
            action="gmail.draft",
            provider="composio:mcp",
            tool_slug="GMAIL_CREATE_EMAIL_DRAFT",
            target="test@test.com",
            data={"id": "draft_1"},
            audited=True,
        )
        d = result.to_dict()
        assert d["success"] is True
        assert d["action"] == "gmail.draft"
        assert d["provider"] == "composio:mcp"
        assert d["tool_slug"] == "GMAIL_CREATE_EMAIL_DRAFT"
        assert d["target"] == "test@test.com"
        assert d["data"]["id"] == "draft_1"
        assert d["audited"] is True
        assert d["error"] is None

    def test_error_result(self):
        from workspace_guardrails import ActionResult
        result = ActionResult(
            success=False,
            action="calendar.create",
            provider="composio:mcp",
            error="Connection failed",
            audited=True,
        )
        d = result.to_dict()
        assert d["success"] is False
        assert d["error"] == "Connection failed"

    def test_default_values(self):
        from workspace_guardrails import ActionResult
        result = ActionResult(success=True)
        d = result.to_dict()
        assert d["action"] == ""
        assert d["data"] == {}
        assert d["audited"] is False


class TestMCPWorkspaceGuardrails:
    """Integration: MCP workspace methods use guardrails and ActionResult."""

    def test_gmail_draft_returns_action_result(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        from unittest.mock import MagicMock

        config = {
            "integrations": {
                "workspace": {
                    "provider": "composio",
                    "mode": "mcp",
                    "user_id": "test",
                    "mcp": {"endpoint": "https://x", "key_env": "COMPOSIO_MCP_KEY"},
                }
            },
            "paths": {"project_root": "/tmp/test-guards"},
        }
        os.environ["COMPOSIO_MCP_KEY"] = "fake"
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"

        client = ComposioMCPWorkspaceClient(config)
        mock_mcp = MagicMock()
        mock_mcp.call_tool.return_value = {
            "data": {"results": [{"response": {"successful": True, "data": {"id": "d1"}}}]},
        }
        client._mcp_client = mock_mcp

        result = client.gmail_create_draft("test@test.com", "Subject", "Body")
        assert result["success"] is True
        assert result["action"] == "gmail.draft"
        assert result["provider"] == "composio:mcp"
        assert result["tool_slug"] == "GMAIL_CREATE_EMAIL_DRAFT"
        assert result["audited"] is True

        os.environ.pop("COMPOSIO_MCP_KEY", None)

    def test_gmail_draft_blocked_by_guardrail(self):
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        from unittest.mock import MagicMock

        config = {
            "integrations": {
                "workspace": {
                    "provider": "composio",
                    "mode": "mcp",
                    "user_id": "test",
                    "mcp": {"endpoint": "https://x", "key_env": "COMPOSIO_MCP_KEY"},
                }
            },
            "paths": {"project_root": "/tmp/test-guards-block"},
        }
        os.environ["COMPOSIO_MCP_KEY"] = "fake"
        os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)  # no auto-approve

        client = ComposioMCPWorkspaceClient(config)
        mock_mcp = MagicMock()
        client._mcp_client = mock_mcp

        result = client.gmail_create_draft("test@test.com", "Subject", "Body")
        assert result["success"] is False
        assert result["error"] == "cancelled by guardrail"
        # MCP should NOT have been called
        mock_mcp.call_tool.assert_not_called()

        os.environ.pop("COMPOSIO_MCP_KEY", None)