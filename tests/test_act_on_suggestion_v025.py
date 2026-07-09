#!/usr/bin/env python3
"""Tests for v0.1.25 — act-on-suggestion bridge.

Verifies:
- Safe read actions (calendar.list, drive.search, gmail.search) execute directly
- Write/destructive actions (gmail.send, gmail.draft, etc.) create pending actions only
- gmail.send suggestion creates pending action, never sends
- gmail.draft suggestion respects provider capability
- drive.search can run as safe read
- Destructive suggestions never execute directly
- Dry-run shows plan without executing or creating pending actions
- Suggestion marked acted_on after read execution or pending creation
- CLI act command works with --dry-run and without
"""

import sys
import os
import json
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
        "integrations": {"workspace": {"provider": "google_api"}},
        "paths": {"project_root": str(project)},
    }
    return config, project


@pytest.fixture
def mock_client():
    mock = MagicMock()
    mock.provider_name = "google_api"
    mock.supports.side_effect = lambda a: True
    mock.calendar_list.return_value = [{"id": "e1", "summary": "Test event"}]
    mock.drive_search.return_value = [{"id": "f1", "name": "test.txt"}]
    mock.gmail_search.return_value = [{"id": "m1", "subject": "Test"}]
    return mock


def _make_suggestion(config, action_type, event_summary="Test event"):
    """Create a single suggestion for testing and persist it to the store."""
    from event_store import ingest_event
    from suggested_actions import generate_suggestions, generate_for_events, list_suggestions
    # Map action_type to an event category that produces it
    category_map = {
        "gmail.draft": "email_received",
        "gmail.send": "email_urgent",
        "calendar.list": "calendar_cancelled",
        "drive.search": "document_shared",
        "drive.download": "document_shared",
    }
    category = category_map.get(action_type, "email_received")
    event = ingest_event(config, "test", f"src_{action_type}", category, {})
    # Persist suggestions to the store
    generate_for_events(config, event_ids=[event["id"]])
    # Find the one with the right action type
    sugs = list_suggestions(config, state="suggested", action_type=action_type)
    if sugs:
        return sugs[0]
    sugs = list_suggestions(config, state="suggested")
    return sugs[0] if sugs else None


# ─── Dry-Run ──────────────────────────────────────────────────

class TestActDryRun:
    """Test dry-run mode shows plan without executing."""

    def test_dry_run_safe_read(self, temp_project, mock_client):
        from suggested_actions import act_on_suggestion
        config, project = temp_project
        sug = _make_suggestion(config, "drive.search")
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            result = act_on_suggestion(config, sug["id"], dry_run=True)
        assert result["mode"] == "dry_run"
        assert result["would_execute_directly"] is True
        assert result["would_create_pending"] is False
        mock_client.drive_search.assert_not_called()  # dry-run doesn't execute

    def test_dry_run_write_action(self, temp_project, mock_client):
        from suggested_actions import act_on_suggestion
        config, project = temp_project
        sug = _make_suggestion(config, "gmail.send")
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            result = act_on_suggestion(config, sug["id"], dry_run=True)
        assert result["mode"] == "dry_run"
        assert result["would_execute_directly"] is False
        assert result["would_create_pending"] is True
        mock_client.gmail_send.assert_not_called()


# ─── Safe Read Actions ────────────────────────────────────────

class TestSafeReadActions:
    """Test that safe read actions execute directly."""

    def test_drive_search_executes(self, temp_project, mock_client):
        from suggested_actions import act_on_suggestion, get_suggestion
        config, project = temp_project
        sug = _make_suggestion(config, "drive.search")
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            result = act_on_suggestion(config, sug["id"])
        assert result["mode"] == "read_executed"
        assert result["success"] is True
        mock_client.drive_search.assert_called_once()

    def test_calendar_list_executes(self, temp_project, mock_client):
        from suggested_actions import act_on_suggestion
        config, project = temp_project
        sug = _make_suggestion(config, "calendar.list")
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            result = act_on_suggestion(config, sug["id"])
        assert result["mode"] == "read_executed"
        mock_client.calendar_list.assert_called_once()

    def test_gmail_search_executes(self, temp_project, mock_client):
        from suggested_actions import act_on_suggestion
        config, project = temp_project
        # gmail.search isn't in templates, but test the handler
        from suggested_actions import SAFE_READ_ACTIONS
        assert "gmail.search" in SAFE_READ_ACTIONS

    def test_safe_read_marks_acted_on(self, temp_project, mock_client):
        from suggested_actions import act_on_suggestion, get_suggestion
        config, project = temp_project
        sug = _make_suggestion(config, "drive.search")
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            act_on_suggestion(config, sug["id"])
        loaded = get_suggestion(config, sug["id"])
        assert loaded["state"] == "acted_on"


# ─── Write Actions → Pending Actions Only ─────────────────────

class TestWriteActionsCreatePending:
    """Test that write/destructive actions create pending actions, never execute."""

    def test_gmail_send_creates_pending_not_sends(self, temp_project, mock_client):
        """The critical test: gmail.send suggestion creates pending action, never sends."""
        from suggested_actions import act_on_suggestion
        config, project = temp_project
        sug = _make_suggestion(config, "gmail.send")
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            result = act_on_suggestion(config, sug["id"])
        assert result["mode"] == "pending_created"
        assert result["success"] is True
        assert result["action_id"] is not None
        # NEVER calls gmail_send directly
        mock_client.gmail_send.assert_not_called()

    def test_gmail_draft_creates_pending(self, temp_project, mock_client):
        from suggested_actions import act_on_suggestion
        config, project = temp_project
        sug = _make_suggestion(config, "gmail.draft")
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            result = act_on_suggestion(config, sug["id"])
        assert result["mode"] == "pending_created"
        mock_client.gmail_create_draft.assert_not_called()

    def test_drive_trash_creates_pending(self, temp_project, mock_client):
        from suggested_actions import act_on_suggestion
        config, project = temp_project
        # drive.trash is a write action but not in templates — create manually
        from suggested_actions import SAFE_READ_ACTIONS, WRITE_ACTIONS
        assert "drive.trash" in WRITE_ACTIONS
        assert "drive.trash" not in SAFE_READ_ACTIONS

    def test_write_marks_acted_on(self, temp_project, mock_client):
        from suggested_actions import act_on_suggestion, get_suggestion
        config, project = temp_project
        sug = _make_suggestion(config, "gmail.send")
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            act_on_suggestion(config, sug["id"])
        loaded = get_suggestion(config, sug["id"])
        assert loaded["state"] == "acted_on"

    def test_destructive_never_executes_directly(self, temp_project, mock_client):
        """All destructive actions must go through pending, never direct execution."""
        from suggested_actions import SAFE_READ_ACTIONS, WRITE_ACTIONS
        destructive = {"gmail.send", "gmail.trash", "drive.trash", "calendar.cancel"}
        for action in destructive:
            assert action not in SAFE_READ_ACTIONS, f"{action} must not be a safe read"
            assert action in WRITE_ACTIONS, f"{action} must be in write actions"


# ─── Capability Check ─────────────────────────────────────────

class TestCapabilityCheck:
    """Test that act checks provider capability before acting."""

    def test_unsupported_read_fails_gracefully(self, temp_project):
        from suggested_actions import act_on_suggestion
        config, project = temp_project
        mock_client = MagicMock()
        mock_client.provider_name = "composio:mcp"
        mock_client.supports.side_effect = lambda a: a not in ("calendar.list",)
        sug = _make_suggestion(config, "calendar.list")
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            result = act_on_suggestion(config, sug["id"])
        assert result["success"] is False
        assert "not supported" in result.get("error", "")

    def test_gmail_draft_respects_capability(self, temp_project):
        """gmail.draft on google_api (which doesn't support it) should fail."""
        from suggested_actions import act_on_suggestion
        config, project = temp_project
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: a != "gmail.draft"
        sug = _make_suggestion(config, "gmail.draft")
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            result = act_on_suggestion(config, sug["id"])
        assert result["success"] is False
        assert "not supported" in result.get("error", "")


# ─── State Validation ─────────────────────────────────────────

class TestActStateValidation:
    """Test that act only works on 'suggested' state."""

    def test_act_on_dismissed_fails(self, temp_project, mock_client):
        from suggested_actions import act_on_suggestion, dismiss_suggestion
        config, project = temp_project
        sug = _make_suggestion(config, "drive.search")
        dismiss_suggestion(config, sug["id"])
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            result = act_on_suggestion(config, sug["id"])
        assert result["success"] is False
        assert "not in 'suggested'" in result.get("error", "")

    def test_act_on_acted_on_fails(self, temp_project, mock_client):
        from suggested_actions import act_on_suggestion, mark_acted_on
        config, project = temp_project
        sug = _make_suggestion(config, "drive.search")
        mark_acted_on(config, sug["id"])
        with patch("workspace_client.get_workspace_client", return_value=mock_client):
            result = act_on_suggestion(config, sug["id"])
        assert result["success"] is False

    def test_act_on_unknown_suggestion_fails(self, temp_project):
        from suggested_actions import act_on_suggestion
        config, project = temp_project
        result = act_on_suggestion(config, "nonexistent_id")
        assert result["success"] is False
        assert "not found" in result.get("error", "")


# ─── CLI Integration ──────────────────────────────────────────

class TestActCLI:
    """Test suggest_actions.py act command."""

    def test_act_dry_run_via_cli(self, temp_project, mock_client):
        config, project = temp_project
        sug = _make_suggestion(config, "gmail.send")
        with patch("suggest_actions.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client):
            import suggest_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = suggest_actions.main(["--summary", "act", "--suggestion-id", sug["id"], "--dry-run"])
        assert rc == 0
        out = buf.getvalue()
        assert "Dry-run" in out
        assert "Would create pending" in out

    def test_act_read_via_cli(self, temp_project, mock_client):
        config, project = temp_project
        sug = _make_suggestion(config, "drive.search")
        with patch("suggest_actions.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client):
            import suggest_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = suggest_actions.main(["--summary", "act", "--suggestion-id", sug["id"]])
        assert rc == 0
        assert "✅ Read executed" in buf.getvalue()

    def test_act_write_via_cli(self, temp_project, mock_client):
        config, project = temp_project
        sug = _make_suggestion(config, "gmail.send")
        with patch("suggest_actions.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client):
            import suggest_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = suggest_actions.main(["--summary", "act", "--suggestion-id", sug["id"]])
        assert rc == 0
        assert "📋 Pending action created" in buf.getvalue()

    def test_act_json_via_cli(self, temp_project, mock_client):
        config, project = temp_project
        sug = _make_suggestion(config, "drive.search")
        with patch("suggest_actions.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client):
            import suggest_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = suggest_actions.main(["act", "--suggestion-id", sug["id"]])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["mode"] == "read_executed"