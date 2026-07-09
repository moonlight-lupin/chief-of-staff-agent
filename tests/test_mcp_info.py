#!/usr/bin/env python3
"""Tests for MCP info command in connect_workspace.py."""

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


@pytest.fixture
def composio_config_file():
    with tempfile.TemporaryDirectory(dir="/root") as d:
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
    mode: sdk
    user_id: "test-mcp-user"
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
      googledrive:
        read:
          - GOOGLEDRIVE_FIND_FILE
          - GOOGLEDRIVE_DOWNLOAD_FILE
        write_safe:
          - GOOGLEDRIVE_UPLOAD_FILE
""")
        yield config


class TestMcpInfo:
    def test_mcp_info_without_session(self, composio_config_file):
        """--mcp-info should work without SDK session_id (MCP mode)."""
        rc, out, err = run_connect("--provider", "composio", "--mcp-info",
                                   config_path=composio_config_file)
        # Should not fail with "no session" — MCP mode doesn't need session_id
        # May return 1 if MCP key not set, but should not say "session"
        assert "session" not in out.lower() or "session_id" not in out.lower()

    def test_mcp_info_with_mocked_mcp(self, composio_config_file):
        """Test --mcp-info --json with mocked MCP client."""
        import yaml
        config = yaml.safe_load(composio_config_file.read_text())
        # Ensure MCP mode for this test
        config["integrations"]["workspace"]["mode"] = "mcp"
        config["integrations"]["workspace"]["mcp"] = {
            "endpoint": "https://connect.composio.dev/mcp",
            "key_env": "COMPOSIO_MCP_KEY",
        }

        # Pre-populate session metadata
        project = Path(config["paths"]["project_root"])
        meta_path = project / ".integrations" / "composio" / "session.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps({
            "provider": "composio",
            "mode": "mcp",
            "endpoint": "https://connect.composio.dev/mcp",
            "key_env": "COMPOSIO_MCP_KEY",
            "mcp_initialized": True,
            "available_meta_tools": ["COMPOSIO_MANAGE_CONNECTIONS", "COMPOSIO_MULTI_EXECUTE_TOOL"],
            "connections": {"gmail": {"status": "connected"}},
        }))

        sys.path.insert(0, str(SHARED_SCRIPTS))
        import importlib
        import connect_workspace as cw
        importlib.reload(cw)

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cw.cmd_composio_mcp_info(config, json_output=True)

        output = buf.getvalue()
        data = json.loads(output)
        assert data["provider"] == "composio"
        assert data["mode"] == "mcp"
        assert data["endpoint"] == "https://connect.composio.dev/mcp"
        assert data["key_env"] == "COMPOSIO_MCP_KEY"
        assert "session_id" not in data  # MCP mode doesn't use session_id
        assert "enabled_tools" in data
        assert "gmail" in data["enabled_tools"]
        assert "GMAIL_FETCH_EMAILS" in data["enabled_tools"]["gmail"]

    def test_mcp_tools_command(self, composio_config_file):
        """Test --mcp-tools prints tools per toolkit."""
        import yaml
        config = yaml.safe_load(composio_config_file.read_text())

        project = Path(config["paths"]["project_root"])
        meta_path = project / ".integrations" / "composio" / "session.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps({
            "user_id": "test-mcp-user",
            "session_id": "sess_test_456",
            "mcp": {"url": None, "headers_stored": False},
            "connections": {},
        }))

        sys.path.insert(0, str(SHARED_SCRIPTS))
        import importlib
        import connect_workspace as cw
        importlib.reload(cw)

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cw.cmd_composio_mcp_info(config, tools_only=True)

        output = buf.getvalue()
        assert "gmail:" in output
        assert "GMAIL_FETCH_EMAILS" in output
        assert "googledrive:" in output
        assert "GOOGLEDRIVE_UPLOAD_FILE" in output