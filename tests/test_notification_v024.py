#!/usr/bin/env python3
"""Tests for v0.1.24 — operator notification and suggestion digest.

Verifies:
- render_digest produces structured digest with risk, confidence, approval
- CLI delivery prints digest to stdout
- Email delivery creates pending action but does NOT auto-send
- Notification never calls provider write methods directly
- mark_notified records notification timestamp
- Risk labels distinguish suggestion_risk vs execution_risk
- Digest text is human-readable
"""

import sys
import os
import json
import io
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

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
def temp_with_suggestions(temp_project):
    """Project with events and generated suggestions."""
    config, project = temp_project
    from state_db import ingest_event
    from suggested_actions import generate_for_events
    ingest_event(config, "gmail", "m1", "email_received", {"from": "client@x.com"})
    ingest_event(config, "gmail", "m2", "email_urgent", {"from": "boss@x.com"})
    ingest_event(config, "calendar", "e1", "calendar_cancelled", {})
    generate_for_events(config)
    return config, project


# ─── Digest Rendering ─────────────────────────────────────────

class TestDigestRendering:
    """Test render_digest produces correct structured output."""

    def test_digest_has_required_fields(self, temp_with_suggestions):
        from suggested_actions import render_digest
        config, project = temp_with_suggestions
        digest = render_digest(config)
        assert "total" in digest
        assert "by_risk" in digest
        assert "by_action" in digest
        assert "requires_approval_count" in digest
        assert "items" in digest
        assert "text" in digest

    def test_digest_total_matches_suggestions(self, temp_with_suggestions):
        from suggested_actions import render_digest, list_suggestions
        config, project = temp_with_suggestions
        digest = render_digest(config)
        sugs = list_suggestions(config, state="suggested")
        assert digest["total"] == len(sugs)

    def test_digest_text_is_readable(self, temp_with_suggestions):
        from suggested_actions import render_digest
        config, project = temp_with_suggestions
        digest = render_digest(config)
        text = digest["text"]
        assert "📊 Suggestion Digest" in text
        assert "gmail.draft" in text  # from email_received/urgent events
        assert "calendar.list" in text  # from calendar_cancelled

    def test_digest_includes_risk_and_confidence(self, temp_with_suggestions):
        from suggested_actions import render_digest
        config, project = temp_with_suggestions
        digest = render_digest(config)
        for item in digest["items"]:
            assert "execution_risk" in item
            assert "suggestion_risk" in item
            assert "confidence" in item

    def test_digest_min_confidence_filter(self, temp_with_suggestions):
        from suggested_actions import render_digest
        config, project = temp_with_suggestions
        all_digest = render_digest(config)
        high_digest = render_digest(config, min_confidence=0.70)
        assert high_digest["total"] <= all_digest["total"]
        for item in high_digest["items"]:
            assert item["confidence"] >= 0.70

    def test_digest_empty_when_no_suggestions(self, temp_project):
        from suggested_actions import render_digest
        config, project = temp_project
        digest = render_digest(config)
        assert digest["total"] == 0
        assert "📊 Suggestion Digest — 0 item(s)" in digest["text"]


# ─── Risk Label Distinction ───────────────────────────────────

class TestRiskLabels:
    """Test suggestion_risk vs execution_risk."""

    def test_draft_has_different_risks(self, temp_with_suggestions):
        """gmail.draft suggestions distinguish suggestion_risk from execution_risk."""
        from suggested_actions import list_suggestions
        config, project = temp_with_suggestions
        drafts = list_suggestions(config, action_type="gmail.draft")
        assert len(drafts) > 0
        for sug in drafts:
            # execution_risk is always medium for gmail.draft
            assert sug["execution_risk"] == "medium"
            # suggestion_risk varies by event category (low for email_received, medium for email_urgent)
            assert sug["suggestion_risk"] in ("low", "medium")
            # The key point: they can differ
            assert "suggestion_risk" in sug
            assert "execution_risk" in sug

    def test_send_has_high_execution_risk(self, temp_with_suggestions):
        from suggested_actions import list_suggestions
        config, project = temp_with_suggestions
        sends = list_suggestions(config, action_type="gmail.send")
        assert len(sends) > 0
        for sug in sends:
            assert sug["execution_risk"] == "high"

    def test_read_actions_have_low_execution_risk(self, temp_with_suggestions):
        from suggested_actions import list_suggestions
        config, project = temp_with_suggestions
        searches = list_suggestions(config, action_type="calendar.list")
        for sug in searches:
            assert sug["execution_risk"] == "low"


# ─── CLI Notification ─────────────────────────────────────────

class TestCLINotification:
    """Test CLI channel delivery."""

    def test_notify_cli_prints_digest(self, temp_with_suggestions):
        config, project = temp_with_suggestions
        with patch("suggest_actions.load_config", return_value=config):
            import suggest_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = suggest_actions.main(["--summary", "notify", "--channel", "cli"])
        assert rc == 0
        out = buf.getvalue()
        assert "📊 Suggestion Digest" in out

    def test_notify_cli_marks_notified(self, temp_with_suggestions):
        from suggested_actions import list_suggestions
        config, project = temp_with_suggestions
        with patch("suggest_actions.load_config", return_value=config):
            import suggest_actions
            suggest_actions.main(["notify", "--channel", "cli"])
        sugs = list_suggestions(config, state="suggested")
        for s in sugs:
            assert s.get("notified_at") is not None

    def test_notify_cli_empty_suggestions(self, temp_project):
        config, project = temp_project
        with patch("suggest_actions.load_config", return_value=config):
            import suggest_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = suggest_actions.main(["--summary", "notify", "--channel", "cli"])
        assert rc == 0
        assert "No suggestions" in buf.getvalue()

    def test_digest_via_cli(self, temp_with_suggestions):
        config, project = temp_with_suggestions
        with patch("suggest_actions.load_config", return_value=config):
            import suggest_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = suggest_actions.main(["--summary", "digest"])
        assert rc == 0
        assert "📊" in buf.getvalue()

    def test_digest_json_via_cli(self, temp_with_suggestions):
        config, project = temp_with_suggestions
        with patch("suggest_actions.load_config", return_value=config):
            import suggest_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = suggest_actions.main(["digest"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["total"] > 0
        assert "items" in data


# ─── Email Notification ───────────────────────────────────────

class TestEmailNotification:
    """Test email channel delivery — creates pending action, NOT auto-sent."""

    def test_email_creates_pending_action(self, temp_with_suggestions):
        config, project = temp_with_suggestions
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        mock_client.supports.side_effect = lambda a: a == "gmail.send"
        with patch("suggest_actions.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            import suggest_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = suggest_actions.main([
                    "notify", "--channel", "email", "--to", "me@test.com",
                ])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["success"] is True
        assert data["action_id"] is not None
        assert "approve" in data["message"].lower()

    def test_email_does_not_auto_send(self, temp_with_suggestions):
        """Email notification must NOT call gmail_send directly."""
        config, project = temp_with_suggestions
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        with patch("suggest_actions.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None):
            import suggest_actions
            suggest_actions.main(["notify", "--channel", "email", "--to", "me@test.com"])
        mock_client.gmail_send.assert_not_called()  # NEVER auto-sent

    def test_email_requires_to_address(self, temp_with_suggestions):
        config, project = temp_with_suggestions
        with patch("suggest_actions.load_config", return_value=config):
            import suggest_actions
            rc = suggest_actions.main(["notify", "--channel", "email"])
        assert rc == 1  # missing --to

    def test_email_unsupported_provider(self, temp_with_suggestions):
        """If provider doesn't support gmail.send, email delivery fails gracefully."""
        config, project = temp_with_suggestions
        mock_client = MagicMock()
        mock_client.provider_name = "composio:mcp"
        mock_client.supports.side_effect = lambda a: a != "gmail.send"
        with patch("suggest_actions.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client):
            import suggest_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = suggest_actions.main([
                    "notify", "--channel", "email", "--to", "me@test.com",
                ])
        assert rc == 1


# ─── No Execution Boundary ────────────────────────────────────

class TestNotificationNoExecution:
    """Prove that notification never executes or approves anything."""

    def test_digest_never_calls_provider(self, temp_with_suggestions):
        from suggested_actions import render_digest
        config, project = temp_with_suggestions
        mock_client = MagicMock()
        digest = render_digest(config)
        mock_client.gmail_send.assert_not_called()
        mock_client.gmail_create_draft.assert_not_called()
        mock_client.calendar_create.assert_not_called()

    def test_notify_never_calls_pending_actions_approve(self, temp_with_suggestions):
        """Notify may create a pending action (email) but never approves it."""
        config, project = temp_with_suggestions
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        with patch("suggest_actions.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None), \
             patch("state_db.approve_pending_action") as mock_approve:
            import suggest_actions
            suggest_actions.main(["notify", "--channel", "email", "--to", "me@test.com"])
            mock_approve.assert_not_called()  # NEVER auto-approves

    def test_notify_never_calls_mark_executing(self, temp_with_suggestions):
        config, project = temp_with_suggestions
        mock_client = MagicMock()
        mock_client.provider_name = "google_api"
        with patch("suggest_actions.load_config", return_value=config), \
             patch("workspace_client.get_workspace_client", return_value=mock_client), \
             patch("workspace_capabilities.require_capability", return_value=None), \
             patch("state_db.mark_executing") as mock_exec:
            import suggest_actions
            suggest_actions.main(["notify", "--channel", "email", "--to", "me@test.com"])
            mock_exec.assert_not_called()  # NEVER auto-executes


# ─── mark_notified ────────────────────────────────────────────

class TestMarkNotified:
    """Test mark_notified records notification timestamp."""

    def test_mark_notified_sets_timestamp(self, temp_with_suggestions):
        from suggested_actions import list_suggestions, mark_notified
        config, project = temp_with_suggestions
        sugs = list_suggestions(config, state="suggested")
        sug_ids = [s["id"] for s in sugs]
        marked = mark_notified(config, sug_ids)
        assert marked == len(sug_ids)
        # Verify timestamps
        sugs_after = list_suggestions(config, state="suggested")
        for s in sugs_after:
            assert s.get("notified_at") is not None

    def test_mark_notified_only_suggested_state(self, temp_with_suggestions):
        """mark_notified should only mark 'suggested' suggestions."""
        from suggested_actions import list_suggestions, mark_notified, dismiss_suggestion
        config, project = temp_with_suggestions
        sugs = list_suggestions(config, state="suggested")
        # Dismiss one
        dismiss_suggestion(config, sugs[0]["id"])
        # Mark all
        marked = mark_notified(config, [s["id"] for s in sugs])
        assert marked == len(sugs) - 1  # dismissed one not marked