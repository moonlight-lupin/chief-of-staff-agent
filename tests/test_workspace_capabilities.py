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

    def test_composio_microsoft_writes_reflect_live_execution(self):
        # Updated to match the LIVE WRITE VERIFICATION run of 2026-07-16 (PR #6,
        # Composio MS Phase 1+2) against a real Outlook + OneDrive connection.
        # A write is True ONLY if it EXECUTED successfully live; the rest stay
        # False with a specific (non-generic) UNSUPPORTED_REASONS entry.
        from workspace_capabilities import get_capabilities, get_unsupported_reason
        caps = get_capabilities("composio_microsoft:mcp")
        # Reads remain execution-verified (v0.3.7 read run).
        assert caps["mail.search"] is True
        assert caps["calendar.list"] is True
        assert caps["files.search"] is True
        # Writes that EXECUTED successfully live on 2026-07-16 → True.
        for action in (
            "mail.draft", "gmail.draft",           # OUTLOOK_CREATE_DRAFT
            "mail.trash", "gmail.trash",            # OUTLOOK_MOVE_MESSAGE → deleteditems
            "calendar.create", "calendar.update", "calendar.delete",
        ):
            assert caps[action] is True, f"{action} should be live-verified True"
        # mail.send stays False by policy (never send).
        assert caps["mail.send"] is False
        # Writes that did NOT execute live stay False, each with a SPECIFIC reason
        # (not the generic "is not supported by ..." fallback).
        generic_suffix = "is not supported by composio_microsoft:mcp"
        for action in (
            # OneDrive write chain — blocked by the FileUploadable/s3key upload arg.
            "files.upload", "drive.upload",
            "files.download", "drive.download",
            "files.trash", "drive.trash",
            # mail-move archive/inbox destinations not exercised (only deleteditems ran).
            "mail.archive", "gmail.archive", "mail.unarchive", "mail.untrash",
        ):
            assert caps[action] is False, f"{action} was not execution-verified; must be False"
            reason = get_unsupported_reason("composio_microsoft:mcp", action)
            assert not reason.endswith(generic_suffix), f"{action} needs a specific reason"
            assert "2026-07-16" in reason

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
