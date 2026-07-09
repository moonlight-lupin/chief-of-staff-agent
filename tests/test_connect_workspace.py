#!/usr/bin/env python3
"""Tests for connect_workspace.py onboarding stub."""

import sys
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "shared" / "scripts" / "connect_workspace.py"


def run_connect(*args, config_path=None):
    cmd = [sys.executable, str(SCRIPT), *args]
    if config_path:
        cmd += ["--config", str(config_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.returncode, result.stdout, result.stderr


@pytest.fixture
def tmp_config():
    with tempfile.TemporaryDirectory(dir="/root") as d:
        config = Path(d) / "company.yaml"
        config.write_text("""\
google:
  service_account_path: "~/.hermes/test.json"
  delegate_email: "founder@test.com"
  account_alias: ""
integrations:
  workspace:
    provider: google_api
    mode: direct
""")
        yield config


class TestConnectWorkspace:
    def test_help(self):
        rc, out, err = run_connect("--help")
        assert rc == 0
        assert "workspace" in out.lower()

    def test_status_outputs_json(self, tmp_config):
        rc, out, err = run_connect("--status", config_path=tmp_config)
        data = __import__("json").loads(out)
        assert "provider" in data
        assert data["provider"] == "google_api"

    def test_composio_prints_next_steps(self):
        rc, out, err = run_connect("--provider", "composio", "--print-next-steps")
        assert rc == 0
        assert "composio" in out.lower() or "Composio" in out
        assert "pip install" in out or "COMPOSIO_API_KEY" in out

    def test_google_api_provider_check(self, tmp_config):
        rc, out, err = run_connect("--provider", "google_api", config_path=tmp_config)
        # May pass or fail depending on auth, but should not crash
        assert rc in (0, 1)
        assert "google" in out.lower() or "Google" in out