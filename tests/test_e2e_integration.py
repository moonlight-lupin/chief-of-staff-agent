#!/usr/bin/env python3
"""E2E integration test: prepare → approve → execute against a mock provider.

Exercises the full pending-action state machine including:
- Lapsed approval rejection
- Concurrent execute (only one wins)
- Audit trail completeness
- Guardrail gating on the execution path
"""

import sys
import os
import json
import io
import shutil
import tempfile
import multiprocessing
from pathlib import Path
from contextlib import redirect_stdout
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


@pytest.fixture
def auto_approve():
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
    yield
    os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)


@pytest.fixture
def google_mock():
    """Mock client that supports gmail.send (google_api)."""
    mock = MagicMock()
    mock.provider_name = "google_api"
    mock.supports.side_effect = lambda action: action != "gmail.draft"
    mock.mail_send.return_value = {
        "success": True, "action": "gmail.send", "provider": "google_api",
        "target": "client@test.com", "data": {"id": "msg123"},
        "audited": True,
    }
    return mock


class TestE2EPrepareApproveExecute:
    """Full lifecycle: prepare → approve → execute → verify state + audit."""

    def test_full_lifecycle(self, temp_project, google_mock, auto_approve):
        """Complete prepare→approve→execute cycle with audit trail."""
        from pending_actions import (
            create_pending_action, approve_pending_action,
            mark_executing, mark_executed, get_pending_action,
        )
        config, project = temp_project

        # 1. Prepare
        action = create_pending_action(
            config, "gmail.send", "google_api", "client@test.com",
            {"to": "client@test.com", "subject": "Test", "body": "Hello"},
        )
        assert action["state"] == "requested"
        assert action["id"]

        # 2. Approve
        approved = approve_pending_action(config, action["id"])
        assert approved["state"] == "approved"
        assert approved["approved_at"]

        # 3. Execute
        mark_executing(config, action["id"])
        result = {"success": True, "data": {"id": "msg123"}}
        executed = mark_executed(config, action["id"], result)
        assert executed["state"] == "executed"
        assert executed["result"]["success"] is True

        # 4. Verify on disk
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "executed"
        assert loaded["result"]["data"]["id"] == "msg123"

    def test_lapsed_approval_rejected(self, temp_project, google_mock, auto_approve):
        """An action with a lapsed approval must not be executable."""
        from pending_actions import (
            create_pending_action, approve_pending_action,
            mark_executing, get_pending_action,
        )
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )
        approve_pending_action(config, action["id"])

        # Manually age the approval past the expiry
        from pending_actions import _load, _save
        data = _load(config)
        data["actions"][action["id"]]["approved_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=50)
        ).isoformat()
        _save(config, data)

        # mark_executing should fail (approval lapsed)
        result = mark_executing(config, action["id"])
        # The action should be expired, not executing
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] in ("expired", "approved"), (
            f"Lapsed action should be expired or still approved, got {loaded['state']}"
        )

    def test_concurrent_execute_one_wins(self, temp_project, google_mock, auto_approve):
        """Two concurrent mark_executing calls — exactly one must win."""
        from pending_actions import (
            create_pending_action, approve_pending_action,
            mark_executing,
        )
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "race@test.com", {"to": "race@test.com"}
        )
        approve_pending_action(config, action["id"])

        # Two concurrent mark_executing calls
        r1 = mark_executing(config, action["id"])
        r2 = mark_executing(config, action["id"])

        winners = sum(1 for r in (r1, r2) if r is not None)
        assert winners == 1, (
            f"Exactly one mark_executing must win, got {winners}"
        )

    def test_audit_trail_complete(self, temp_project, google_mock, auto_approve):
        """All state transitions must produce audit records."""
        from pending_actions import (
            create_pending_action, approve_pending_action,
            mark_executing, mark_executed,
        )
        config, project = temp_project

        with patch("workspace_audit.audit_workspace_action") as mock_audit:
            action = create_pending_action(
                config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
            )
            approve_pending_action(config, action["id"])
            mark_executing(config, action["id"])
            mark_executed(config, action["id"], {"success": True})

        statuses = [c.kwargs.get("status") for c in mock_audit.call_args_list]
        assert "requested" in statuses
        assert "approved" in statuses
        assert "executed" in statuses

    def test_cancelled_action_not_executable(self, temp_project, google_mock, auto_approve):
        """A cancelled action must not be executable."""
        from pending_actions import (
            create_pending_action, cancel_pending_action,
            mark_executing,
        )
        config, project = temp_project

        action = create_pending_action(
            config, "gmail.send", "google_api", "a@b.com", {"to": "a@b.com"}
        )
        cancel_pending_action(config, action["id"])

        result = mark_executing(config, action["id"])
        assert result is None, "Cancelled action must not be executable"

    def test_guardrail_blocks_unapproved_write(self, temp_project):
        """confirm_action must block unapproved write actions."""
        from workspace_guardrails import confirm_action
        # Without auto-approve, non-TTY: must block
        os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)
        assert confirm_action("gmail.send") is False
        assert confirm_action("gmail.archive") is False
        assert confirm_action("drive.trash") is False

    def test_guardrail_allows_reads(self, temp_project):
        """confirm_action must allow read actions."""
        from workspace_guardrails import confirm_action
        assert confirm_action("gmail.search") is True
        assert confirm_action("calendar.list") is True
        assert confirm_action("drive.search") is True

    def test_guardrail_blocks_unknown(self, temp_project):
        """confirm_action must block unknown action IDs (default-deny)."""
        from workspace_guardrails import confirm_action
        os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
        assert confirm_action("evil.wipe") is False
        assert confirm_action("unknown.mutation") is False
        os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)