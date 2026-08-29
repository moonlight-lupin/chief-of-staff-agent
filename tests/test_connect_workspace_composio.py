#!/usr/bin/env python3
"""Tests for connect_workspace.py Composio commands — all mocked."""

import sys
import os
import tempfile
import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "shared" / "scripts" / "connect_workspace.py"
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


def run_connect(*args, config_path=None):
    cmd = [sys.executable, str(SCRIPT), *args]
    if config_path:
        cmd += ["--config", str(config_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.returncode, result.stdout, result.stderr


def run_connect_hermetic(*args, config_path=None, env_overrides=None):
    """Run connect_workspace.py in a subprocess with COMPOSIO_MCP_KEY scrubbed.

    Field briefing 2026-08-29: connect_workspace.py auto-loads the plugin-root
    .env, so on any deployment where COMPOSIO_MCP_KEY is legitimately set (the
    normal configured state) the "missing-key" tests silently pass through the
    production key path and assert the wrong branch. This runner deletes the
    variable from the subprocess environment so the no-key branch is exercised
    hermetically, regardless of the operator's local configuration.
    """
    cmd = [sys.executable, str(SCRIPT), *args]
    if config_path:
        cmd += ["--config", str(config_path)]
    env = os.environ.copy()
    env.pop("COMPOSIO_MCP_KEY", None)
    env.pop("COMPOSIO_API_KEY", None)
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
    return result.returncode, result.stdout, result.stderr


@pytest.fixture
def composio_config_file():
    with tempfile.TemporaryDirectory() as d:
        config = Path(d) / "company.yaml"
        project = Path(d) / "project"
        project.mkdir()
        config.write_text(f"""\
company:
  name: "Test Co"
  jurisdiction: SG
  incorporation_date: "2024-01-15"
  financial_year_end: "31 Dec"
  currency: SGD
google:
  delegate_email: "founder@test.com"
  account_alias: ""
paths:
  project_root: "{project}"
  wiki_path: "{project}/wiki/"
delivery:
  channel: telegram
  briefing_time: "20:00"
  timezone: "Asia/Singapore"
integrations:
  workspace:
    provider: composio
    mode: mcp
    user_id: "test-user-123"
    mcp:
      endpoint: "https://connect.composio.dev/mcp"
      key_env: "COMPOSIO_MCP_KEY"
    toolkits:
      - gmail
      - googlecalendar
      - googledrive
    tools_allowlist:
      gmail:
        read:
          - GMAIL_FETCH_EMAILS
        write_safe:
          - GMAIL_CREATE_EMAIL_DRAFT
      googlecalendar:
        read:
          - GOOGLECALENDAR_FIND_EVENT
        write_safe:
          - GOOGLECALENDAR_CREATE_EVENT
""")
        yield config


class TestConnectWorkspaceComposio:
    def test_help_lists_connect_and_mcp(self):
        rc, out, err = run_connect("--help")
        assert rc == 0
        assert "--connect" in out
        assert "--mcp-url" in out

    def test_status_with_composio_config(self, composio_config_file):
        rc, out, err = run_connect("--status", config_path=composio_config_file)
        data = json.loads(out)
        assert data["provider"] == "composio"
        assert "mcp_key_set" in data

    def test_composio_provider_info(self, composio_config_file):
        rc, out, err = run_connect("--provider", "composio", config_path=composio_config_file)
        assert rc in (0, 1)  # may fail if no API key
        assert "COMPOSIO" in out or "composio" in out.lower()

    def test_composio_connect_gmail_without_api_key(self, composio_config_file):
        os.environ.pop("COMPOSIO_MCP_KEY", None)
        rc, out, err = run_connect("--provider", "composio", "--connect", "gmail",
                                   config_path=composio_config_file)
        assert rc == 1
        assert "COMPOSIO_MCP_KEY" in out or "COMPOSIO_API_KEY" not in out

    def test_composio_connect_gmail_with_mock(self, composio_config_file):
        """Test --connect gmail with mocked Composio SDK."""
        os.environ["COMPOSIO_MCP_KEY"] = "fake-key"

        # We can't easily mock through subprocess, so test via direct import
        sys.path.insert(0, str(SHARED_SCRIPTS))
        import importlib
        import connect_workspace as cw
        importlib.reload(cw)

        import yaml
        config = yaml.safe_load(composio_config_file.read_text())
        config.setdefault("integrations", {}).setdefault("workspace", {}).setdefault("mcp", {})
        config["integrations"]["workspace"]["mode"] = "mcp"

        # Mock ComposioMCPWorkspaceClient
        mock_client = MagicMock()
        mock_client.endpoint = "https://connect.composio.dev/mcp"
        mock_client._manage_connections.return_value = {
            "results": {"gmail": {"redirect_url": "https://composio.dev/connect/abc123", "accounts": []}}
        }

        with patch("providers.composio_mcp_workspace.ComposioMCPWorkspaceClient", return_value=mock_client):
            rc = cw.cmd_composio_connect(config, "gmail")

        assert rc == 0
        mock_client._manage_connections.assert_called_once()

        os.environ.pop("COMPOSIO_MCP_KEY", None)

    def test_mcp_url_without_session(self, composio_config_file):
        rc, out, err = run_connect("--provider", "composio", "--mcp-url",
                                   config_path=composio_config_file)
        # Should fail since no session exists
        assert rc == 1
        assert "session" in out.lower() or "connect" in out.lower()

    def test_connect_gmail_without_api_key_hermetic(self, composio_config_file):
        """No-key branch must hold even when the operator's .env sets the key.

        Field briefing 2026-08-29: connect_workspace.py auto-loads the
        plugin-root .env. When COMPOSIO_MCP_KEY is set there (normal
        configured state), the non-hermetic variant of this test passes
        through the production key path and asserts the wrong branch.
        This variant scrubs the key from the subprocess env AND points the
        config at a guaranteed-unset key_env, so the missing-key refusal is
        what is actually under test.
        """
        hermetic_cfg = composio_config_file.with_name("company_hermetic.yaml")
        hermetic_cfg.write_text(
            composio_config_file.read_text().replace(
                "COMPOSIO_MCP_KEY", "COMPOSIO_TEST_KEY_NOT_SET"
            )
        )
        rc, out, err = run_connect_hermetic(
            "--provider", "composio", "--connect", "gmail", config_path=hermetic_cfg
        )
        assert rc == 1
        assert "COMPOSIO_TEST_KEY_NOT_SET" in out

    def test_mcp_url_without_session_hermetic(self, composio_config_file):
        """--mcp-url with no key available must refuse with the key env named.

        Same hermeticity concern as above: without scrubbing, a production
        key in .env lets --mcp-url proceed to initialize() and the test
        asserts the wrong branch.
        """
        hermetic_cfg = composio_config_file.with_name("company_hermetic.yaml")
        hermetic_cfg.write_text(
            composio_config_file.read_text().replace(
                "COMPOSIO_MCP_KEY", "COMPOSIO_TEST_KEY_NOT_SET"
            )
        )
        rc, out, err = run_connect_hermetic(
            "--provider", "composio", "--mcp-url", config_path=hermetic_cfg
        )
        assert rc == 1
        assert "COMPOSIO_TEST_KEY_NOT_SET" in out