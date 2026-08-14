#!/usr/bin/env python3
"""Contract tests for Phase 2 Loop 2: tasks 2.7, 2.8, 2.9, 2.11.

2.7: Backup retention — prune .backups/ to N most recent / M days
2.8: Audit strict whitespace fix — strip stores in CHIEF_OF_STAFF_AUDIT_STRICT
2.9: Fail loudly on missing project root — _project_root should raise
2.11: MCP session recovery — check notifications/initialized response, reset on failure
"""

import sys
import os
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture
def temp_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = {
        "google": {"delegate_email": "test@test.com", "account_alias": "test"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "paths": {"project_root": str(project)},
    }
    return config, project


# ═══════════════════════════════════════════════════════════════
# 2.7: Backup retention
# ═══════════════════════════════════════════════════════════════

class TestBackupRetention:
    """save_store_atomic must prune .backups/ to a bounded number of files.

    Currently .backups/ grows without bound. Each save copies the old file
    to .backups/{store}.{timestamp}.yaml but nothing prunes old backups.
    """

    def test_backups_pruned_after_save(self, temp_project):
        """After a save, .backups/ must not exceed MAX_BACKUPS (default 20)."""
        from state_store import load_store, save_store_atomic
        config, project = temp_project

        # Create 25 backups by saving 25 times
        for i in range(25):
            data = load_store("pipeline", config=config)
            data["deals"].append({"id": f"deal-{i}", "name": f"Deal {i}"})
            save_store_atomic("pipeline", data, config=config)

        backup_dir = project / ".backups"
        if not backup_dir.exists():
            pytest.skip("Backup dir not created — save_store_atomic may not create backups for new stores")

        backup_files = list(backup_dir.glob("pipeline.*.yaml"))
        # Must be pruned to a reasonable max (20 default)
        max_backups = 20
        assert len(backup_files) <= max_backups, (
            f"Backups not pruned: {len(backup_files)} files, expected <= {max_backups}"
        )

    def test_backups_pruned_by_age(self, temp_project):
        """Backups older than MAX_BACKUP_DAYS (default 30) must be pruned."""
        from state_store import load_store, save_store_atomic
        config, project = temp_project

        # Create a backup
        data = load_store("pipeline", config=config)
        data["deals"].append({"id": "deal-1", "name": "Deal 1"})
        save_store_atomic("pipeline", data, config=config)

        # Manually create an old backup (40 days ago)
        backup_dir = project / ".backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        old_time = (datetime.now(timezone.utc) - timedelta(days=40))
        old_name = f"pipeline.{old_time.strftime('%Y%m%dT%H%M%S.%fZ')}.yaml"
        old_file = backup_dir / old_name
        old_file.write_text("deals: []")

        # Trigger another save (should prune the old backup)
        data = load_store("pipeline", config=config)
        data["deals"].append({"id": "deal-2", "name": "Deal 2"})
        save_store_atomic("pipeline", data, config=config)

        # The old backup should be gone
        assert not old_file.exists(), (
            "Old backup (40 days) should have been pruned after save"
        )


# ═══════════════════════════════════════════════════════════════
# 2.8: Audit strict whitespace fix
# ═══════════════════════════════════════════════════════════════

class TestAuditStrictWhitespace:
    """CHIEF_OF_STAFF_AUDIT_STRICT split must strip whitespace.

    Currently: `"pipeline, invoices".split(",")` gives
    `["pipeline", " invoices"]` — the second store name has a leading
    space and silently fails to match.
    """

    def test_strict_stores_with_whitespace(self, temp_project):
        """Setting CHIEF_OF_STAFF_AUDIT_STRICT='pipeline, invoices' must match both."""
        from state_store import load_store, save_store_atomic, StateStoreError
        config, project = temp_project

        # Initialize the store
        data = load_store("pipeline", config=config)
        data["deals"].append({"id": "deal-1", "name": "Deal 1"})
        save_store_atomic("pipeline", data, config=config)

        # Now save with strict mode for "pipeline, invoices" (with space)
        os.environ["CHIEF_OF_STAFF_AUDIT_STRICT"] = "pipeline, invoices"
        try:
            data = load_store("pipeline", config=config)
            data["deals"].append({"id": "deal-2", "name": "Deal 2"})

            # Patch append_audit to fail — strict mode should raise
            with patch("state_store.append_audit",
                       side_effect=Exception("audit DB down")):
                with pytest.raises(StateStoreError, match="strict mode"):
                    save_store_atomic("pipeline", data, config=config)
        finally:
            os.environ.pop("CHIEF_OF_STAFF_AUDIT_STRICT", None)

    def test_strict_stores_no_whitespace(self, temp_project):
        """Setting CHIEF_OF_STAFF_AUDIT_STRICT='pipeline,invoices' (no space) must also work."""
        from state_store import load_store, save_store_atomic, StateStoreError
        config, project = temp_project

        data = load_store("pipeline", config=config)
        data["deals"].append({"id": "deal-1", "name": "Deal 1"})
        save_store_atomic("pipeline", data, config=config)

        os.environ["CHIEF_OF_STAFF_AUDIT_STRICT"] = "pipeline,invoices"
        try:
            data = load_store("pipeline", config=config)
            data["deals"].append({"id": "deal-2", "name": "Deal 2"})

            with patch("state_store.append_audit",
                       side_effect=Exception("audit DB down")):
                with pytest.raises(StateStoreError, match="strict mode"):
                    save_store_atomic("pipeline", data, config=config)
        finally:
            os.environ.pop("CHIEF_OF_STAFF_AUDIT_STRICT", None)


# ═══════════════════════════════════════════════════════════════
# 2.9: Fail loudly on missing project root
# ═══════════════════════════════════════════════════════════════

class TestProjectRootFailLoudly:
    """_project_root in pending_actions should raise, not silently fall back.

    Currently it falls back to ~/.hermes/projects/default when config is
    missing. This can silently write state files to the wrong location.
    """

    def test_missing_project_root_raises(self, tmp_path):
        """pending_actions._project_root must raise when no root is configured."""
        from pending_actions import _project_root

        # Clear all project root env vars
        env_backup = {}
        for key in ("CHIEF_OF_STAFF_PROJECT_ROOT", "CHIEF_OF_STAFF_HERMES_HOME", "HERMES_HOME"):
            if key in os.environ:
                env_backup[key] = os.environ.pop(key)

        try:
            # Config with no paths.project_root
            config = {"google": {"delegate_email": "test@test.com"}}
            with pytest.raises((RuntimeError, ValueError, Exception)):
                _project_root(config)
        finally:
            for key, val in env_backup.items():
                os.environ[key] = val


# ═══════════════════════════════════════════════════════════════
# 2.11: MCP session recovery
# ═══════════════════════════════════════════════════════════════

class TestMCPSessionRecovery:
    """MCP client must check notifications/initialized response and recover.

    Currently it fires notifications/initialized and ignores the HTTP
    response. If the handshake fails, the client marks itself initialized
    anyway — subsequent calls will fail with confusing errors.
    """

    def test_initialized_notification_checks_response(self):
        """MCPClient.initialize must check the response of notifications/initialized."""
        from mcp_client import MCPClient

        os.environ["FAKE_KEY"] = "fake-key-12345"
        client = MCPClient("https://fake.example.com/mcp", key_env="FAKE_KEY")

        # Mock the initialize response (session ID present)
        mock_init_response = MagicMock()
        mock_init_response.status_code = 200
        mock_init_response.headers = {"mcp-session-id": "test-session-123"}
        mock_init_response.text = '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{}}}'

        # Mock the initialized notification response to return an error
        mock_notif_response = MagicMock()
        mock_notif_response.status_code = 500

        with patch("requests.post",
                   side_effect=[mock_init_response, mock_notif_response]):
            # initialize() should detect the failed notification and raise/reset
            with pytest.raises((ConnectionError, RuntimeError, Exception)):
                client.initialize()

        os.environ.pop("FAKE_KEY", None)

    def test_initialized_notification_success(self):
        """MCPClient.initialize must succeed when notifications/initialized returns 200."""
        from mcp_client import MCPClient

        os.environ["FAKE_KEY"] = "fake-key-12345"
        client = MCPClient("https://fake.example.com/mcp", key_env="FAKE_KEY")

        mock_init_response = MagicMock()
        mock_init_response.status_code = 200
        mock_init_response.headers = {"mcp-session-id": "test-session-456"}
        mock_init_response.text = '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{}}}'

        mock_notif_response = MagicMock()
        mock_notif_response.status_code = 200

        with patch("requests.post",
                   side_effect=[mock_init_response, mock_notif_response]):
            result = client.initialize()

        assert client._initialized is True
        assert result is not None

        os.environ.pop("FAKE_KEY", None)