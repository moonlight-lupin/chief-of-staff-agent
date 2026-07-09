#!/usr/bin/env python3
"""Tests for workspace_client abstraction and Google provider."""

import sys
import os
import tempfile
import json
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture
def google_config():
    """Config with google_api provider (default)."""
    return {
        "google": {
            "service_account_path": "~/.hermes/test.json",
            "domain": "test.com",
            "delegate_email": "founder@test.com",
            "account_alias": "",
        },
        "integrations": {
            "workspace": {
                "provider": "google_api",
                "mode": "direct",
            }
        }
    }


@pytest.fixture
def composio_config():
    """Config with composio provider."""
    return {
        "integrations": {
            "workspace": {
                "provider": "composio",
                "mode": "sdk",
            }
        }
    }


@pytest.fixture
def no_integrations_config():
    """Config with no integrations section (should default to google_api)."""
    return {
        "google": {
            "delegate_email": "founder@test.com",
        }
    }


class TestWorkspaceClientFactory:
    def test_returns_google_client_for_google_api(self, google_config):
        from workspace_client import get_workspace_client, WorkspaceClient
        from providers.google_workspace import GoogleWorkspaceClient
        client = get_workspace_client(google_config)
        assert isinstance(client, GoogleWorkspaceClient)
        assert isinstance(client, WorkspaceClient)

    def test_raises_for_composio(self, composio_config):
        from workspace_client import get_workspace_client
        with pytest.raises(NotImplementedError, match="Composio"):
            get_workspace_client(composio_config)

    def test_defaults_to_google_when_no_integrations(self, no_integrations_config):
        from workspace_client import get_workspace_client
        from providers.google_workspace import GoogleWorkspaceClient
        client = get_workspace_client(no_integrations_config)
        assert isinstance(client, GoogleWorkspaceClient)

    def test_unknown_provider_raises(self):
        from workspace_client import get_workspace_client
        with pytest.raises(ValueError, match="Unknown workspace provider"):
            get_workspace_client({"integrations": {"workspace": {"provider": "unknown"}}})


class TestWorkspaceClientABC:
    def test_cannot_instantiate_abc_directly(self):
        from workspace_client import WorkspaceClient
        with pytest.raises(TypeError):
            WorkspaceClient()


class TestGoogleWorkspaceClient:
    def test_gmail_search_returns_list(self, google_config):
        from providers.google_workspace import GoogleWorkspaceClient
        client = GoogleWorkspaceClient(google_config)
        # Will likely return empty list if no auth, but should not crash
        result = client.gmail_search("is:unread", max_results=1)
        assert isinstance(result, list)

    def test_calendar_list_returns_list(self, google_config):
        from providers.google_workspace import GoogleWorkspaceClient
        client = GoogleWorkspaceClient(google_config)
        result = client.calendar_list("2026-01-01", "2026-01-02")
        assert isinstance(result, list)

    def test_drive_search_returns_list(self, google_config):
        from providers.google_workspace import GoogleWorkspaceClient
        client = GoogleWorkspaceClient(google_config)
        result = client.drive_search("test", max_results=1)
        assert isinstance(result, list)

    def test_health_check_returns_bool(self, google_config):
        from providers.google_workspace import GoogleWorkspaceClient
        client = GoogleWorkspaceClient(google_config)
        result = client.health_check()
        assert isinstance(result, bool)

    def test_account_alias_passed_to_cmd(self, google_config):
        google_config["google"]["account_alias"] = "mycompany"
        from providers.google_workspace import GoogleWorkspaceClient
        client = GoogleWorkspaceClient(google_config)
        cmd = client._build_cmd("calendar", "list")
        assert "--account" in cmd
        assert "mycompany" in cmd

    def test_no_account_alias_omits_flag(self, google_config):
        from providers.google_workspace import GoogleWorkspaceClient
        client = GoogleWorkspaceClient(google_config)
        cmd = client._build_cmd("calendar", "list")
        assert "--account" not in cmd