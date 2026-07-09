#!/usr/bin/env python3
"""Tests for workspace_client abstraction and Google provider.

Tests monkeypatch the google_api.py lookup and subprocess calls so they
work in any environment — no Google skill installation required.
"""

import sys
import os
import tempfile
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

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
        with patch("providers.google_workspace._find_google_api_script", return_value=Path("/fake/google_api.py")):
            client = get_workspace_client(google_config)
        assert isinstance(client, GoogleWorkspaceClient)
        assert isinstance(client, WorkspaceClient)

    @pytest.fixture
    def composio_config(self):
        """Config with composio provider and user_id."""
        return {
            "integrations": {
                "workspace": {
                    "provider": "composio",
                    "mode": "sdk",
                    "user_id": "test-user-123",
                    "toolkits": ["gmail", "googlecalendar", "googledrive"],
                }
            },
            "paths": {
                "project_root": "/tmp/test-composio",
            },
        }

    def test_returns_composio_client(self, composio_config):
        from workspace_client import get_workspace_client
        from providers.composio_workspace import ComposioWorkspaceClient
        client = get_workspace_client(composio_config)
        assert isinstance(client, ComposioWorkspaceClient)

    def test_defaults_to_google_when_no_integrations(self, no_integrations_config):
        from workspace_client import get_workspace_client
        from providers.google_workspace import GoogleWorkspaceClient
        with patch("providers.google_workspace._find_google_api_script", return_value=Path("/fake/google_api.py")):
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
    @pytest.fixture
    def client(self, google_config):
        """GoogleWorkspaceClient with google_api.py path monkeypatched."""
        from providers.google_workspace import GoogleWorkspaceClient
        with patch("providers.google_workspace._find_google_api_script", return_value=Path("/fake/google_api.py")):
            return GoogleWorkspaceClient(google_config)

    def test_gmail_search_returns_list(self, client):
        with patch.object(client, "_run", return_value=(0, "[]", "")):
            result = client.gmail_search("is:unread", max_results=1)
        assert isinstance(result, list)

    def test_gmail_search_parses_json(self, client):
        mock_response = json.dumps([{"id": "msg1", "subject": "Test"}])
        with patch.object(client, "_run", return_value=(0, mock_response, "")):
            result = client.gmail_search("is:unread", max_results=5)
        assert len(result) == 1
        assert result[0]["id"] == "msg1"

    def test_gmail_search_returns_empty_on_error(self, client):
        with patch.object(client, "_run", return_value=(1, "", "auth failed")):
            result = client.gmail_search("is:unread")
        assert result == []

    def test_calendar_list_returns_list(self, client):
        with patch.object(client, "_run", return_value=(0, "[]", "")):
            result = client.calendar_list("2026-01-01", "2026-01-02")
        assert isinstance(result, list)

    def test_calendar_list_parses_json(self, client):
        mock_response = json.dumps([{"id": "evt1", "title": "Meeting"}])
        with patch.object(client, "_run", return_value=(0, mock_response, "")):
            result = client.calendar_list("2026-01-01", "2026-01-02")
        assert len(result) == 1
        assert result[0]["title"] == "Meeting"

    def test_drive_search_returns_list(self, client):
        with patch.object(client, "_run", return_value=(0, "[]", "")):
            result = client.drive_search("test", max_results=1)
        assert isinstance(result, list)

    def test_health_check_returns_true_on_success(self, client):
        with patch.object(client, "_run", return_value=(0, "[]", "")):
            assert client.health_check() is True

    def test_health_check_returns_false_on_failure(self, client):
        with patch.object(client, "_run", return_value=(1, "", "error")):
            assert client.health_check() is False

    def test_account_alias_passed_to_cmd(self, google_config):
        google_config["google"]["account_alias"] = "mycompany"
        from providers.google_workspace import GoogleWorkspaceClient
        with patch("providers.google_workspace._find_google_api_script", return_value=Path("/fake/google_api.py")):
            client = GoogleWorkspaceClient(google_config)
        cmd = client._build_cmd("calendar", "list")
        assert "--account" in cmd
        assert "mycompany" in cmd

    def test_no_account_alias_omits_flag(self, client):
        cmd = client._build_cmd("calendar", "list")
        assert "--account" not in cmd

    def test_delegate_email_passed_to_cmd(self, client):
        cmd = client._build_cmd("calendar", "list")
        assert "--as" in cmd
        assert "founder@test.com" in cmd