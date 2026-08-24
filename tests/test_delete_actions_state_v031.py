#!/usr/bin/env python3
"""Tests for v0.3.1 — delete_actions execute-path state integrity.

Verifies delete_actions.cmd_execute mirrors webhook_events.cmd_execute:
- A provider ActionResult with success=False marks the action FAILED
  (back to 'approved' for retry with last_error), never 'executed'.
- A provider exception marks the action FAILED (back to 'approved').
- An unsupported capability is refused BEFORE the provider method runs
  (require_capability gate), returning the action to 'approved' with last_error.
- A provider success=True still marks the action 'executed' (regression guard).
"""

import sys
import os
import io
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILLS_SCRIPTS = PLUGIN_ROOT / "skills"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
for skill in ("document-preparer",):
    d = SKILLS_SCRIPTS / skill / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


@pytest.fixture
def temp_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = {
        "google": {"delegate_email": "test@test.com", "account_alias": "test",
                   "domain": "phronesis-applied.com"},
        "company": {"website": "phronesis-applied.com"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "paths": {"project_root": str(project)},
    }
    return config, project


@pytest.fixture
def auto_approve():
    os.environ["CHIEF_OF_STAFF_AUTO_APPROVE"] = "1"
    yield
    os.environ.pop("CHIEF_OF_STAFF_AUTO_APPROVE", None)


def _prepare_approved(config, action_type, target, provider_method):
    """Create a soft-delete action already in 'approved' state."""
    from state_db import create_pending_action, approve_pending_action
    action = create_pending_action(
        config, action_type, "google_api", target,
        {"reason": "test", "reversible": True,
         "restore_hint": "restore hint", "provider_method": provider_method},
    )
    approve_pending_action(config, action["id"])
    return action


# ─── (a) Provider returns success=False ───────────────────────

class TestProviderReturnsFailure:
    """A provider ActionResult with success=False must mark_failed, not mark_executed."""

    def test_provider_failure_marks_failed_not_executed(self, temp_project, auto_approve):
        config, project = temp_project
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda action: True
        mock_client.gmail_archive.return_value = {
            "success": False, "action": "gmail.archive", "provider": "google_api",
            "target": "msg1", "error": "provider boom", "audited": True,
        }
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=mock_client):
            import delete_actions
            from state_db import get_pending_action
            action = _prepare_approved(config, "gmail.archive", "msg1", "gmail_archive")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = delete_actions.main(["execute", "--action-id", action["id"]])
        # Provider WAS invoked (this is a real failure, not a capability refusal)
        mock_client.gmail_archive.assert_called_once_with("msg1")
        # Nonzero exit
        assert rc == 1
        loaded = get_pending_action(config, action["id"])
        # mark_failed returns the action to 'approved' for retry, records error
        assert loaded["state"] == "approved"
        assert loaded["last_error"] == "provider boom"
        assert loaded["retry_count"] == 1
        # NOT recorded as executed
        assert loaded.get("executed_at") is None
        assert loaded.get("result") is None

    def test_provider_failure_without_error_uses_default_message(self, temp_project, auto_approve):
        config, project = temp_project
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda action: True
        mock_client.drive_trash.return_value = {"success": False}
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=mock_client):
            import delete_actions
            from state_db import get_pending_action
            action = _prepare_approved(config, "drive.trash", "file1", "drive_trash")
            rc = delete_actions.main(["execute", "--action-id", action["id"]])
        assert rc == 1
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "approved"
        assert loaded["last_error"] == "provider returned failure"


# ─── (b) Provider raises ──────────────────────────────────────

class TestProviderRaises:
    """A provider exception must mark_failed (back to approved), aligned with webhook_events."""

    def test_provider_exception_marks_failed(self, temp_project, auto_approve):
        config, project = temp_project
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda action: True
        mock_client.calendar_cancel.side_effect = Exception("Graph 500")
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=mock_client):
            import delete_actions
            from state_db import get_pending_action
            action = _prepare_approved(config, "calendar.cancel", "evt1", "calendar_cancel")
            rc = delete_actions.main(["execute", "--action-id", action["id"]])
        assert rc == 1
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "approved"
        assert "Graph 500" in loaded.get("last_error", "")
        assert loaded.get("executed_at") is None


# ─── (c) Unsupported capability ───────────────────────────────

class TestCapabilityRefused:
    """An unsupported capability must be refused BEFORE the provider method runs."""

    def test_unsupported_capability_refused_before_provider_call(self, temp_project, auto_approve):
        config, project = temp_project
        mock_client = MagicMock()
        mock_client.provider_name = "m365"
        # m365 does not support calendar.cancel (no restore path).
        mock_client.supports.side_effect = lambda action: action != "calendar.cancel"
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=mock_client):
            import delete_actions
            from state_db import get_pending_action
            action = _prepare_approved(config, "calendar.cancel", "evt1", "calendar_cancel")
            rc = delete_actions.main(["execute", "--action-id", action["id"]])
        # Refused before the provider method was ever invoked
        mock_client.calendar_cancel.assert_not_called()
        assert rc == 1
        loaded = get_pending_action(config, action["id"])
        # mark_failed returned it to 'approved' with the capability last_error
        assert loaded["state"] == "approved"
        assert loaded["last_error"] == "calendar.cancel not supported by m365"
        assert loaded.get("executed_at") is None
        assert loaded.get("result") is None

    def test_m365_config_client_refuses_calendar_cancel(self, temp_project, auto_approve):
        """Capability refusal using the real capability matrix (not a hand-mocked supports)."""
        config, project = temp_project
        # Real supports() consulting the m365 capability matrix.
        from workspace_capabilities import supports as caps_supports
        mock_client = MagicMock()
        mock_client.provider_name = "m365"
        mock_client.supports.side_effect = lambda action: caps_supports("m365", action)
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=mock_client):
            import delete_actions
            from state_db import get_pending_action
            action = _prepare_approved(config, "calendar.cancel", "evt1", "calendar_cancel")
            rc = delete_actions.main(["execute", "--action-id", action["id"]])
        mock_client.calendar_cancel.assert_not_called()
        assert rc == 1
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "approved"
        assert loaded["last_error"] == "calendar.cancel not supported by m365"


# ─── (d) Provider success=True (regression guard) ─────────────

class TestProviderSuccess:
    """A provider success=True still marks executed as before."""

    def test_success_marks_executed(self, temp_project, auto_approve):
        config, project = temp_project
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda action: True
        mock_client.gmail_archive.return_value = {
            "success": True, "action": "gmail.archive", "provider": "google_api",
            "target": "msg1", "data": {"reversible": True}, "audited": True,
        }
        with patch("delete_actions.load_config", return_value=config), \
             patch("delete_actions.get_client", return_value=mock_client):
            import delete_actions
            from state_db import get_pending_action
            action = _prepare_approved(config, "gmail.archive", "msg1", "gmail_archive")
            rc = delete_actions.main(["execute", "--action-id", action["id"]])
        assert rc == 0
        mock_client.gmail_archive.assert_called_once_with("msg1")
        loaded = get_pending_action(config, action["id"])
        assert loaded["state"] == "executed"
        assert loaded["executed_at"] is not None
        assert loaded["result"]["success"] is True
        assert loaded.get("last_error") is None
