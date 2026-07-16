#!/usr/bin/env python3
"""Tests for workspace_capabilities.py."""

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


class TestCapabilities:
    def test_google_api_capabilities(self):
        from workspace_capabilities import get_capabilities
        caps = get_capabilities("google_api")
        assert caps["gmail.search"] is True
        assert caps["gmail.draft"] is False       # google_api.py has no draft subcommand
        assert caps["gmail.send"] is True          # supported but destructive
        assert caps["calendar.create"] is True
        assert caps["drive.upload"] is True

    def test_composio_capabilities(self):
        from workspace_capabilities import get_capabilities
        caps = get_capabilities("composio")
        assert caps["gmail.search"] is True
        assert caps["gmail.draft"] is True
        assert caps["gmail.send"] is False
        assert caps["drive.search"] is True

    def test_composio_microsoft_writes_include_files_and_mail_moves(self):
        # v0.3.10: OneDrive FileUploadable staging + archive/inbox moves are
        # capability-True (wired + unit-tested). mail.send / tags / cancel stay
        # False by policy / Phase 4.
        from workspace_capabilities import get_capabilities, get_unsupported_reason
        caps = get_capabilities("composio_microsoft:mcp")
        assert caps["mail.search"] is True
        assert caps["calendar.list"] is True
        assert caps["files.search"] is True
        for action in (
            "mail.draft", "gmail.draft",
            "mail.trash", "gmail.trash",
            "mail.archive", "gmail.archive", "mail.unarchive", "mail.untrash",
            "calendar.create", "calendar.update", "calendar.delete",
            "files.upload", "drive.upload",
            "files.download", "drive.download",
            "files.trash", "drive.trash",
        ):
            assert caps[action] is True, f"{action} should be supported"
        assert caps["mail.send"] is False
        assert "intentionally disabled" in get_unsupported_reason(
            "composio_microsoft:mcp", "mail.send"
        )
        for action in ("mail.tag", "mail.create_tag", "calendar.cancel"):
            assert caps[action] is False

    def test_supports(self):
        from workspace_capabilities import supports
        assert supports("google_api", "gmail.send") is True
        assert supports("composio", "gmail.send") is False
        assert supports("composio", "drive.upload") is True

    def test_unsupported_actions(self):
        from workspace_capabilities import unsupported_actions
        google_unsup = unsupported_actions("google_api")
        assert "gmail.draft" in google_unsup  # now False
        assert "gmail.send" not in google_unsup
        composio_unsup = unsupported_actions("composio")
        assert "gmail.send" in composio_unsup

    def test_unknown_provider_returns_empty(self):
        from workspace_capabilities import get_capabilities, supports
        assert get_capabilities("unknown") == {}
        assert supports("unknown", "anything") is False

    def test_all_actions(self):
        from workspace_capabilities import all_actions
        actions = all_actions()
        assert "gmail.search" in actions

    def test_workflow_requirements_exist(self):
        from workspace_capabilities import WORKFLOW_REQUIREMENTS
        assert "document.handoff" in WORKFLOW_REQUIREMENTS
        assert "meeting.gather" in WORKFLOW_REQUIREMENTS
        assert "weekly.collect" in WORKFLOW_REQUIREMENTS

    def test_unsupported_reasons_exist(self):
        from workspace_capabilities import UNSUPPORTED_REASONS
        assert ("google_api", "gmail.draft") in UNSUPPORTED_REASONS
        assert ("composio:mcp", "gmail.send") in UNSUPPORTED_REASONS

    def test_provider_recommendations_exist(self):
        from workspace_capabilities import PROVIDER_RECOMMENDATIONS
        assert PROVIDER_RECOMMENDATIONS["gmail.draft"] == "composio"
        assert PROVIDER_RECOMMENDATIONS["document.handoff"] == "composio"
