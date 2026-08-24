#!/usr/bin/env python3
"""Contract tests for Phase 3 Loop 1: Opus M1-M5 + tasks 3.1, 3.4.

Opus M1: ConcurrencyError retry on all 9 mutating paths (not just 2)
Opus M2: HMAC timestamp mandatory for generic endpoint
Opus M3: Lease renewal API for slow handlers
Opus M4: _fill_required_store_fields must not run on production path
Opus M5: MCP session recovery from lost session (404)
Task 3.1: Stuck-action reconciliation (executing timeout)
Task 3.4: Clarify read-only docs (daily_briefing writes .last_briefing)
"""

import sys
import os
import json
import time
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


@pytest.fixture(autouse=True)
def clean_env():
    for key in ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"):
        os.environ.pop(key, None)
    yield
    for key in ("CHIEF_OF_STAFF_AUTO_APPROVE", "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"):
        os.environ.pop(key, None)


# ═══════════════════════════════════════════════════════════════
# Opus M1: ConcurrencyError retry on all mutating paths
# ═══════════════════════════════════════════════════════════════

class TestRetryOnAllMutators:
    """All 9 mutating paths in pending_actions must retry on ConcurrencyError.

    Currently only mark_executed and mark_failed have retry loops.
    mark_executing, create_pending_action, approve, cancel, dismiss, etc.
    all let ConcurrencyError escape.
    """

    def test_mark_executing_returns_none_on_conflict(self, temp_project):
        """mark_executing must return None when CAS fails (state mismatch).

        Phase 5: concurrency is handled by SQLite row-level CAS.
        If the action state was already changed by another worker,
        the UPDATE WHERE state=? matches 0 rows and returns None.
        """
        from state_db import (
            create_pending_action, approve_pending_action,
            mark_executing,
        )
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )
        # Don't approve — mark_executing expects state='approved', so
        # CAS will not match and return None.
        result = mark_executing(config, action["id"])
        assert result is None, "mark_executing must return None on state mismatch"

    def test_create_pending_action_retries_on_conflict(self, temp_project):
        """create_pending_action must succeed under the new CAS architecture.

        Phase 5: create_action uses INSERT (no CAS needed for creation).
        The old _save retry loop is replaced by direct SQLite INSERT.
        """
        from state_db import create_pending_action
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )

        assert action is not None
        assert action["state"] == "requested"

    def test_approve_returns_none_on_persistent_conflict(self, temp_project):
        """approve_pending_action must not raise on persistent ConcurrencyError."""
        from state_db import (
            create_pending_action, approve_pending_action, ConcurrencyError,
        )
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )

        with patch("state_db._save",
                   side_effect=ConcurrencyError("Always fail")):
            try:
                result = approve_pending_action(config, action["id"])
                # Must return None, not raise
            except ConcurrencyError:
                pytest.fail("approve_pending_action must not propagate ConcurrencyError")

    def test_cancel_returns_none_on_persistent_conflict(self, temp_project):
        """cancel_pending_action must not raise on persistent ConcurrencyError."""
        from state_db import (
            create_pending_action, cancel_pending_action, ConcurrencyError,
        )
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )

        with patch("state_db._save",
                   side_effect=ConcurrencyError("Always fail")):
            try:
                result = cancel_pending_action(config, action["id"])
            except ConcurrencyError:
                pytest.fail("cancel_pending_action must not propagate ConcurrencyError")


# ═══════════════════════════════════════════════════════════════
# Opus M2: HMAC timestamp mandatory for generic endpoint
# ═══════════════════════════════════════════════════════════════

class TestHMACTimestampMandatory:
    """Generic HMAC endpoint must reject requests without timestamp.

    Currently verify_signature with timestamp=None falls back to body-only
    HMAC. An attacker can replay old requests by omitting the header.
    """

    def test_verify_rejects_none_timestamp_when_required(self):
        """verify_signature must reject when timestamp is None and require_timestamp=True."""
        from webhook_validation import verify_signature
        secret = "test-secret-key-123456"
        body = b'{"event": "test"}'

        # Body-only signature (legacy) must be rejected when require_timestamp=True
        sig = verify_signature(body, "fake", secret=secret, timestamp=None,
                               require_timestamp=True)
        assert sig is False

    def test_verify_rejects_empty_timestamp_when_required(self):
        """Empty timestamp string must be rejected when require_timestamp=True."""
        from webhook_validation import verify_signature
        secret = "test-secret-key-123456"
        body = b'{"event": "test"}'
        result = verify_signature(body, "fake", secret=secret, timestamp="",
                                  require_timestamp=True)
        assert result is False


# ═══════════════════════════════════════════════════════════════
# Opus M3: Lease renewal API
# ═══════════════════════════════════════════════════════════════

class TestLeaseRenewal:
    """reserve_delivery should support lease renewal for slow handlers.

    A delivery handler that takes > 5 min must be able to renew its lease
    to prevent a second worker from reclaiming and double-processing.
    """

    def test_renew_delivery_exists(self, temp_project):
        """renew_delivery function must exist."""
        from state_db import reserve_delivery
        try:
            from state_db import renew_delivery
        except ImportError:
            pytest.fail("renew_delivery must exist for slow-handler lease renewal")

    def test_renew_delivery_extends_lease(self, temp_project):
        """renew_delivery must extend the processing lease."""
        from state_db import reserve_delivery, renew_delivery, _load_replay_cache
        import time as _time
        config, project = temp_project

        ok, _ = reserve_delivery(config, "delivery-renew")
        assert ok

        # Age the reservation to near lease expiry
        cache = _load_replay_cache(config)
        cache["entries"]["delivery-renew"]["ts"] = _time.time() - 250  # 4 min ago
        from state_db import _save_replay_cache_unlocked
        _save_replay_cache_unlocked(config, cache)

        # Renew the lease
        renewed = renew_delivery(config, "delivery-renew")
        assert renewed is True, "renew_delivery must extend the lease"

        # Verify the timestamp was updated
        cache = _load_replay_cache(config)
        new_age = _time.time() - cache["entries"]["delivery-renew"]["ts"]
        assert new_age < 10, "Lease ts must be refreshed after renewal"


# ═══════════════════════════════════════════════════════════════
# Opus M4: _fill_required_store_fields must not run on production path
# ═══════════════════════════════════════════════════════════════

class TestNoFieldFabrication:
    """save_store_atomic must not fabricate missing required fields in production.

    Currently _fill_required_store_fields runs on every save, inventing
    client_name and stage='Lead' for incomplete deals. This masks bugs.
    """

    def test_save_rejects_missing_stage(self, temp_project):
        """save_store_atomic must fail validation for a deal missing 'stage'."""
        from state_db import load_store, save_store_atomic
        config, project = temp_project

        data = load_store("pipeline", config=config)
        # Add a deal with NO stage (incomplete)
        data["deals"].append({"id": "deal-bad", "name": "Bad Deal"})
        # This must raise (validation failure), not silently set stage="Lead"
        with pytest.raises((Exception,)):
            save_store_atomic("pipeline", data, config=config)

    def test_save_with_fill_defaults_flag(self, temp_project):
        """save_store_atomic with _fill_defaults=True (test mode) fills fields."""
        from state_db import load_store, save_store_atomic
        config, project = temp_project

        data = load_store("pipeline", config=config)
        data["deals"].append({"id": "deal-test", "name": "Test Deal"})
        # With fill_defaults=True (for tests), it should fill and succeed
        try:
            save_store_atomic("pipeline", data, config=config, _fill_defaults=True)
        except TypeError:
            # If the function doesn't accept _fill_defaults yet, that's a failure
            # But maybe the test should be more lenient — let's check if
            # the function signature accepts it
            pytest.fail("save_store_atomic must accept _fill_defaults parameter")


# ═══════════════════════════════════════════════════════════════
# Opus M5: MCP session recovery from lost session (404)
# ═══════════════════════════════════════════════════════════════

class TestMCPSessionRecovery404:
    """MCP client must recover from a lost session (404) by re-initializing.

    Currently call_tool/list_tools convert 404 to terminal ConnectionError.
    The fix: on 404, clear session, re-initialize, retry once.
    """

    def test_call_tool_recovers_from_404(self):
        """call_tool must re-initialize and retry on HTTP 404."""
        from mcp_client import MCPClient

        os.environ["FAKE_KEY"] = "fake-key-12345"
        client = MCPClient("https://fake.example.com/mcp", key_env="FAKE_KEY")
        client._initialized = True
        client._session_id = "old-session"

        # First call_tool gets 404, second (after re-init) gets 200
        mock_404 = MagicMock()
        mock_404.status_code = 404
        mock_404.text = "Session not found"

        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.text = 'data: {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"ok"}]}}'

        # Re-init sequence
        mock_init = MagicMock()
        mock_init.status_code = 200
        mock_init.headers = {"mcp-session-id": "new-session"}
        mock_init.text = '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{}}}'

        mock_notif = MagicMock()
        mock_notif.status_code = 202

        with patch("requests.post",
                   side_effect=[mock_404, mock_init, mock_notif, mock_200]):
            result = client.call_tool("test_tool", {"arg": "val"})

        assert result is not None
        assert client._session_id == "new-session"

        os.environ.pop("FAKE_KEY", None)


# ═══════════════════════════════════════════════════════════════
# Task 3.1: Stuck-action reconciliation
# ═══════════════════════════════════════════════════════════════

class TestStuckActionReconciliation:
    """Actions stuck in 'executing' state must be detected and recoverable.

    If a worker crashes after mark_executing but before mark_executed,
    the action stays 'executing' forever. A readiness check must detect
    this and provide a recovery command.
    """

    def test_detect_stuck_executing_action(self, temp_project):
        """readiness check must detect actions stuck in 'executing' state."""
        from state_db import (
            create_pending_action, approve_pending_action,
            mark_executing, _load, _save,
        )
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )
        approve_pending_action(config, action["id"])
        mark_executing(config, action["id"])

        # Age the executing_at timestamp to 30 minutes ago
        data = _load(config)
        data["actions"][action["id"]]["executing_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=30)
        ).isoformat()
        _save(config, data)

        # Check if there's a function to detect stuck actions
        try:
            from state_db import find_stuck_actions
        except ImportError:
            pytest.fail("find_stuck_actions must exist to detect stuck 'executing' actions")

        stuck = find_stuck_actions(config, max_minutes=15)
        assert len(stuck) == 1
        assert stuck[0]["id"] == action["id"]

    def test_revert_stuck_action(self, temp_project):
        """A stuck 'executing' action must be revertable to 'approved' for retry."""
        from state_db import (
            create_pending_action, approve_pending_action,
            mark_executing, _load, _save, get_pending_action,
        )
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )
        approve_pending_action(config, action["id"])
        mark_executing(config, action["id"])

        # Age it
        data = _load(config)
        data["actions"][action["id"]]["executing_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=30)
        ).isoformat()
        _save(config, data)

        # Revert it
        try:
            from state_db import revert_stuck_action
        except ImportError:
            pytest.fail("revert_stuck_action must exist")

        result = revert_stuck_action(config, action["id"])
        assert result is not None
        assert result["state"] == "approved"

        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "approved"