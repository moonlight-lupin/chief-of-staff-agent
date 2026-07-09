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
        assert caps["gmail.draft"] is True
        assert caps["calendar.create"] is True
        assert caps["drive.upload"] is True

    def test_composio_capabilities(self):
        from workspace_capabilities import get_capabilities
        caps = get_capabilities("composio")
        assert caps["gmail.search"] is True
        assert caps["gmail.draft"] is True
        assert caps["gmail.send"] is False
        assert caps["drive.search"] is True

    def test_supports(self):
        from workspace_capabilities import supports
        assert supports("google_api", "gmail.send") is True
        assert supports("composio", "gmail.send") is False
        assert supports("composio", "drive.upload") is True

    def test_unsupported_actions(self):
        from workspace_capabilities import unsupported_actions
        google_unsup = unsupported_actions("google_api")
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
        assert "calendar.create" in actions
        assert "drive.upload" in actions