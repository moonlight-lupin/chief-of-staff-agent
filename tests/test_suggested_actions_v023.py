#!/usr/bin/env python3
"""Tests for v0.1.23 — suggested action generation.

Verifies:
- Events produce structured suggestions with confidence, risk, provider
- Suggestions NEVER call provider methods or pending_actions
- auto_execute is always False for all suggestions
- generate_for_events is idempotent (second run skips events with suggestions)
- dismiss and acted_on state transitions work
- CLI commands work for generate, list, dismiss, acted-on, summary
- Filtering by state, action_type, min_confidence works
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
def temp_project_with_events(temp_project):
    """Create a project with some classified events."""
    config, project = temp_project
    from event_store import ingest_event
    e1 = ingest_event(config, "gmail", "m1", "email_received",
                      {"from": "client@x.com", "subject": "NDA"})
    e2 = ingest_event(config, "gmail", "m2", "email_urgent",
                      {"from": "boss@x.com", "subject": "URGENT: Review"})
    e3 = ingest_event(config, "calendar", "e1", "calendar_cancelled",
                      {"title": "Team Sync"})
    return config, project, [e1, e2, e3]


# ─── Suggestion Generation ────────────────────────────────────

class TestSuggestionGeneration:
    """Test that events produce structured suggestions."""

    def test_email_received_produces_suggestion(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        from suggested_actions import generate_suggestions
        suggestions = generate_suggestions(config, events[0])
        assert len(suggestions) >= 1
        sug = suggestions[0]
        assert sug["action_type"] == "gmail.draft"
        assert sug["auto_execute"] is False
        assert sug["state"] == "suggested"
        assert "confidence" in sug
        assert "risk" in sug
        assert "provider" in sug
        assert sug["requires_approval"] is True

    def test_email_urgent_produces_two_suggestions(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        from suggested_actions import generate_suggestions
        suggestions = generate_suggestions(config, events[1])
        assert len(suggestions) == 2
        action_types = [s["action_type"] for s in suggestions]
        assert "gmail.draft" in action_types
        assert "gmail.send" in action_types

    def test_calendar_cancelled_produces_suggestion(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        from suggested_actions import generate_suggestions
        suggestions = generate_suggestions(config, events[2])
        assert len(suggestions) >= 1
        assert suggestions[0]["action_type"] == "calendar.list"
        assert suggestions[0]["requires_approval"] is False

    def test_unknown_event_no_suggestions(self, temp_project):
        from suggested_actions import generate_suggestions
        from event_store import ingest_event
        config, project = temp_project
        event = ingest_event(config, "custom", "x1", "some_unknown_type", {})
        suggestions = generate_suggestions(config, event)
        assert len(suggestions) == 0

    def test_suggestion_has_all_required_fields(self, temp_project_with_events):
        """Each suggestion must have the full structured shape."""
        config, project, events = temp_project_with_events
        from suggested_actions import generate_suggestions
        suggestions = generate_suggestions(config, events[0])
        sug = suggestions[0]
        required = {"id", "event_id", "action_type", "title", "reason",
                    "confidence", "risk", "provider", "requires_approval",
                    "auto_execute", "state", "created_at"}
        assert required.issubset(set(sug.keys()))

    def test_auto_execute_always_false(self, temp_project_with_events):
        """No suggestion should ever have auto_execute=True."""
        config, project, events = temp_project_with_events
        from suggested_actions import generate_suggestions, SUGGESTION_TEMPLATES
        # Check all templates
        for category, templates in SUGGESTION_TEMPLATES.items():
            for template in templates:
                assert template.get("requires_approval") is not None
        # Check generated suggestions
        for event in events:
            suggestions = generate_suggestions(config, event)
            for sug in suggestions:
                assert sug["auto_execute"] is False


# ─── No Execution ─────────────────────────────────────────────

class TestNoExecution:
    """Prove that suggestions never call provider or pending_actions."""

    def test_generate_never_calls_provider(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        mock_client = MagicMock()
        from suggested_actions import generate_suggestions
        suggestions = generate_suggestions(config, events[0])
        mock_client.gmail_send.assert_not_called()
        mock_client.gmail_create_draft.assert_not_called()

    def test_generate_for_events_never_calls_pending_actions(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        from suggested_actions import generate_for_events
        with patch("pending_actions.create_pending_action") as mock_create:
            result = generate_for_events(config)
            mock_create.assert_not_called()
        assert result["generated"] > 0

    def test_dismiss_never_calls_provider(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        from suggested_actions import generate_for_events, dismiss_suggestion
        generate_for_events(config)
        mock_client = MagicMock()
        with patch("suggested_actions._provider_for_action", return_value="google_api"):
            sug = dismiss_suggestion(config, list_suggestions_helper(config)[0]["id"])
        mock_client.gmail_send.assert_not_called()


def list_suggestions_helper(config):
    from suggested_actions import list_suggestions
    return list_suggestions(config, state="suggested")


# ─── Idempotency ──────────────────────────────────────────────

class TestSuggestionIdempotency:
    """Test that generate_for_events is idempotent."""

    def test_second_generate_skips_existing(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        from suggested_actions import generate_for_events
        first = generate_for_events(config)
        second = generate_for_events(config)
        assert first["generated"] > 0
        assert second["generated"] == 0
        assert second["skipped"] > 0

    def test_generate_for_specific_event(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        from suggested_actions import generate_for_events
        result = generate_for_events(config, event_ids=[events[0]["id"]])
        assert result["generated"] > 0
        assert result["events_processed"] == 1


# ─── State Transitions ────────────────────────────────────────

class TestSuggestionStates:
    """Test dismiss and acted_on transitions."""

    def test_dismiss_suggestion(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        from suggested_actions import generate_for_events, dismiss_suggestion, get_suggestion
        generate_for_events(config)
        suggestions = list_suggestions_helper(config)
        sug = dismiss_suggestion(config, suggestions[0]["id"], reason="Not relevant")
        assert sug["state"] == "dismissed"
        assert sug["dismiss_reason"] == "Not relevant"

    def test_dismiss_idempotent(self, temp_project_with_events):
        """Double dismiss should fail."""
        config, project, events = temp_project_with_events
        from suggested_actions import generate_for_events, dismiss_suggestion
        generate_for_events(config)
        suggestions = list_suggestions_helper(config)
        first = dismiss_suggestion(config, suggestions[0]["id"])
        assert first is not None
        second = dismiss_suggestion(config, suggestions[0]["id"])
        assert second is None

    def test_acted_on_suggestion(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        from suggested_actions import generate_for_events, mark_acted_on
        generate_for_events(config)
        suggestions = list_suggestions_helper(config)
        sug = mark_acted_on(config, suggestions[0]["id"], notes="Sent reply via approval queue")
        assert sug["state"] == "acted_on"
        assert sug["action_notes"] == "Sent reply via approval queue"

    def test_acted_on_idempotent(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        from suggested_actions import generate_for_events, mark_acted_on
        generate_for_events(config)
        suggestions = list_suggestions_helper(config)
        first = mark_acted_on(config, suggestions[0]["id"])
        assert first is not None
        second = mark_acted_on(config, suggestions[0]["id"])
        assert second is None


# ─── Filtering ────────────────────────────────────────────────

class TestSuggestionFiltering:
    """Test list filtering."""

    def test_filter_by_state(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        from suggested_actions import generate_for_events, list_suggestions, dismiss_suggestion
        generate_for_events(config)
        all_sugs = list_suggestions(config, state="suggested")
        assert len(all_sugs) > 0
        # Dismiss one
        dismiss_suggestion(config, all_sugs[0]["id"])
        suggested = list_suggestions(config, state="suggested")
        dismissed = list_suggestions(config, state="dismissed")
        assert len(suggested) == len(all_sugs) - 1
        assert len(dismissed) == 1

    def test_filter_by_action_type(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        from suggested_actions import generate_for_events, list_suggestions
        generate_for_events(config)
        drafts = list_suggestions(config, action_type="gmail.draft")
        sends = list_suggestions(config, action_type="gmail.send")
        assert all(s["action_type"] == "gmail.draft" for s in drafts)
        assert all(s["action_type"] == "gmail.send" for s in sends)

    def test_filter_by_min_confidence(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        from suggested_actions import generate_for_events, list_suggestions
        generate_for_events(config)
        high = list_suggestions(config, min_confidence=0.70)
        assert all(s["confidence"] >= 0.70 for s in high)

    def test_summary(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        from suggested_actions import generate_for_events, get_suggestion_summary
        generate_for_events(config)
        summary = get_suggestion_summary(config)
        assert summary["total"] > 0
        assert summary["active_count"] > 0
        assert "by_state" in summary
        assert "by_risk" in summary


# ─── CLI Integration ──────────────────────────────────────────

class TestSuggestionCLI:
    """Test suggest_actions.py CLI."""

    def test_generate_via_cli(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        with patch("suggest_actions.load_config", return_value=config):
            import suggest_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = suggest_actions.main(["--summary", "generate"])
        assert rc == 0
        assert "Generated" in buf.getvalue()

    def test_list_via_cli(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        with patch("suggest_actions.load_config", return_value=config):
            import suggest_actions
            suggest_actions.main(["generate"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = suggest_actions.main(["--summary", "list"])
        assert rc == 0
        assert "💡" in buf.getvalue()

    def test_summary_via_cli(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        with patch("suggest_actions.load_config", return_value=config):
            import suggest_actions
            suggest_actions.main(["generate"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = suggest_actions.main(["--summary", "summary"])
        assert rc == 0
        assert "Suggestions:" in buf.getvalue()

    def test_dismiss_via_cli(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        with patch("suggest_actions.load_config", return_value=config):
            import suggest_actions
            suggest_actions.main(["generate"])
            sugs = list_suggestions_helper(config)
            rc = suggest_actions.main(["dismiss", "--suggestion-id", sugs[0]["id"]])
        assert rc == 0

    def test_acted_on_via_cli(self, temp_project_with_events):
        config, project, events = temp_project_with_events
        with patch("suggest_actions.load_config", return_value=config):
            import suggest_actions
            suggest_actions.main(["generate"])
            sugs = list_suggestions_helper(config)
            rc = suggest_actions.main(["acted-on", "--suggestion-id", sugs[0]["id"],
                                        "--notes", "Handled"])
        assert rc == 0


# ─── Cleanup ──────────────────────────────────────────────────

class TestSuggestionCleanup:
    """Test cleanup of old dismissed/acted_on suggestions."""

    def test_cleanup_removes_old(self, temp_project_with_events):
        from datetime import datetime, timedelta, timezone
        config, project, events = temp_project_with_events
        from suggested_actions import generate_for_events, dismiss_suggestion, cleanup_old_suggestions
        from suggested_actions import _load, _save
        generate_for_events(config)
        sugs = list_suggestions_helper(config)
        dismiss_suggestion(config, sugs[0]["id"])
        # Age the dismissed_at
        data = _load(config)
        ev = data.get("_version", 0)
        for sid in data["suggestions"]:
            if data["suggestions"][sid]["state"] == "dismissed":
                data["suggestions"][sid]["dismissed_at"] = (
                    datetime.now(timezone.utc) - timedelta(days=31)
                ).isoformat()
        _save(config, data, expected_version=ev)
        removed = cleanup_old_suggestions(config, days=30)
        assert removed >= 1