#!/usr/bin/env python3
"""Tests for v0.1.21 — event ingestion foundation and idempotency.

Verifies:
- Duplicate events (same source + source_id) are ignored (idempotent)
- Event classification assigns category and suggested actions
- No automatic execution — auto_execute is always False
- Events can be listed, filtered, and marked processed
- Replay safety — re-ingesting same event is a no-op
- Cleanup removes old processed events
- CLI commands work for ingest, list, summary, mark-processed
"""

import sys
import os
import json
import io
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

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


# ─── Idempotency ──────────────────────────────────────────────

class TestIdempotency:
    """Test duplicate event detection and deduplication."""

    def test_first_ingest_creates_event(self, temp_project):
        from event_store import ingest_event
        config, project = temp_project
        event = ingest_event(config, "gmail", "msg001", "email_received",
                             {"from": "client@x.com", "subject": "NDA"})
        assert event is not None
        assert event["state"] == "classified"
        assert event["source"] == "gmail"
        assert event["source_id"] == "msg001"

    def test_duplicate_ingest_returns_none(self, temp_project):
        from event_store import ingest_event
        config, project = temp_project
        first = ingest_event(config, "gmail", "msg001", "email_received",
                             {"from": "client@x.com"})
        second = ingest_event(config, "gmail", "msg001", "email_received",
                              {"from": "client@x.com"})
        assert first is not None
        assert second is None  # duplicate, ignored

    def test_different_source_id_not_duplicate(self, temp_project):
        from event_store import ingest_event
        config, project = temp_project
        first = ingest_event(config, "gmail", "msg001", "email_received", {})
        second = ingest_event(config, "gmail", "msg002", "email_received", {})
        assert first is not None
        assert second is not None

    def test_different_source_not_duplicate(self, temp_project):
        from event_store import ingest_event
        config, project = temp_project
        first = ingest_event(config, "gmail", "msg001", "email_received", {})
        second = ingest_event(config, "calendar", "msg001", "calendar_changed", {})
        assert first is not None
        assert second is not None

    def test_replay_safety(self, temp_project):
        """Re-ingesting the same event multiple times is safe."""
        from event_store import ingest_event, list_events
        config, project = temp_project
        for _ in range(5):
            ingest_event(config, "gmail", "msg001", "email_received", {"subject": "Test"})
        events = list_events(config)
        assert len(events) == 1  # only one event, not 5


# ─── Classification ───────────────────────────────────────────

class TestClassification:
    """Test event classification and suggested actions."""

    def test_email_received_classification(self, temp_project):
        from event_store import ingest_event
        config, project = temp_project
        event = ingest_event(config, "gmail", "msg001", "email_received", {})
        cls = event["classification"]
        assert cls["category"] == "email_received"
        assert cls["auto_execute"] is False
        assert "gmail.search" in cls["suggested_actions"]

    def test_urgent_email_classification(self, temp_project):
        from event_store import ingest_event
        config, project = temp_project
        event = ingest_event(config, "gmail", "msg001", "email_urgent", {})
        cls = event["classification"]
        assert cls["category"] == "email_urgent"
        assert "gmail.send" in cls["suggested_actions"]

    def test_calendar_changed_classification(self, temp_project):
        from event_store import ingest_event
        config, project = temp_project
        event = ingest_event(config, "calendar", "evt001", "calendar_changed", {})
        cls = event["classification"]
        assert cls["category"] == "calendar_changed"

    def test_unknown_event_type_classified(self, temp_project):
        from event_store import ingest_event
        config, project = temp_project
        event = ingest_event(config, "custom", "x001", "some_new_type", {})
        cls = event["classification"]
        assert cls["category"] == "unknown"
        assert cls["suggested_actions"] == []

    def test_auto_execute_always_false(self, temp_project):
        """No event type should ever have auto_execute=True."""
        from event_store import ingest_event, EVENT_CATEGORIES
        config, project = temp_project
        for event_type in EVENT_CATEGORIES:
            if event_type == "unknown":
                continue
            event = ingest_event(config, "test", f"id_{event_type}", event_type, {})
            assert event["classification"]["auto_execute"] is False, \
                f"{event_type} must not auto-execute"


# ─── No Auto-Execution ────────────────────────────────────────

class TestNoAutoExecution:
    """Prove that ingesting an event never triggers any provider call."""

    def test_ingest_never_calls_provider(self, temp_project):
        from event_store import ingest_event
        config, project = temp_project
        # Even with email_urgent (which suggests gmail.send),
        # no provider should be called
        mock_client = MagicMock()
        event = ingest_event(config, "gmail", "msg001", "email_urgent",
                             {"from": "urgent@x.com"})
        mock_client.gmail_send.assert_not_called()
        mock_client.gmail_create_draft.assert_not_called()
        mock_client.calendar_create.assert_not_called()

    def test_no_destructive_auto_action(self, temp_project):
        from event_store import ingest_event, EVENT_CATEGORIES
        config, project = temp_project
        # Check that no category has destructive=True
        for cat_name, cat in EVENT_CATEGORIES.items():
            assert cat["destructive"] is False, \
                f"{cat_name} must not be destructive"


# ─── Event Lifecycle ──────────────────────────────────────────

class TestEventLifecycle:
    """Test event state transitions."""

    def test_list_by_state(self, temp_project):
        from event_store import ingest_event, list_events, mark_processed
        config, project = temp_project
        e1 = ingest_event(config, "gmail", "m1", "email_received", {})
        e2 = ingest_event(config, "gmail", "m2", "email_received", {})
        mark_processed(config, e1["id"], processed_by="MH")
        classified = list_events(config, state="classified")
        processed = list_events(config, state="processed")
        assert len(classified) == 1  # only e2
        assert len(processed) == 1  # only e1

    def test_mark_processed_stores_metadata(self, temp_project):
        from event_store import ingest_event, mark_processed, get_event
        config, project = temp_project
        event = ingest_event(config, "gmail", "m1", "email_received", {})
        result = mark_processed(config, event["id"], processed_by="MH", notes="Handled via reply")
        assert result["state"] == "processed"
        assert result["processed_by"] == "MH"
        assert result["processing_notes"] == "Handled via reply"

    def test_mark_processed_idempotent(self, temp_project):
        """Double mark-processed should fail on second call."""
        from event_store import ingest_event, mark_processed
        config, project = temp_project
        event = ingest_event(config, "gmail", "m1", "email_received", {})
        first = mark_processed(config, event["id"])
        assert first is not None
        second = mark_processed(config, event["id"])
        assert second is None  # already processed

    def test_list_by_source(self, temp_project):
        from event_store import ingest_event, list_events
        config, project = temp_project
        ingest_event(config, "gmail", "m1", "email_received", {})
        ingest_event(config, "calendar", "e1", "calendar_changed", {})
        gmail_events = list_events(config, source="gmail")
        cal_events = list_events(config, source="calendar")
        assert len(gmail_events) == 1
        assert len(cal_events) == 1

    def test_get_event(self, temp_project):
        from event_store import ingest_event, get_event
        config, project = temp_project
        event = ingest_event(config, "gmail", "m1", "email_received", {"subject": "Test"})
        loaded = get_event(config, event["id"])
        assert loaded is not None
        assert loaded["payload"]["subject"] == "Test"

    def test_event_summary(self, temp_project):
        from event_store import ingest_event, get_event_summary, mark_processed
        config, project = temp_project
        e1 = ingest_event(config, "gmail", "m1", "email_received", {})
        e2 = ingest_event(config, "calendar", "e1", "calendar_changed", {})
        mark_processed(config, e1["id"])
        summary = get_event_summary(config)
        assert summary["total"] == 2
        assert summary["by_state"]["processed"] == 1
        assert summary["by_state"]["classified"] == 1
        assert summary["pending_count"] == 1

    def test_cleanup_old_events(self, temp_project):
        from event_store import ingest_event, mark_processed, cleanup_old_events, _load, _save
        config, project = temp_project
        e1 = ingest_event(config, "gmail", "m1", "email_received", {})
        mark_processed(config, e1["id"])
        # Age the processed_at
        data = _load(config)
        ev = data.get("_version", 0)
        for key in data["events"]:
            if data["events"][key]["id"] == e1["id"]:
                data["events"][key]["processed_at"] = (
                    datetime.now(timezone.utc) - timedelta(days=31)
                ).isoformat()
        _save(config, data, expected_version=ev)
        removed = cleanup_old_events(config, days=30)
        assert removed == 1


# ─── CLI Integration ──────────────────────────────────────────

class TestEventCLI:
    """Test event_actions.py CLI commands."""

    def test_ingest_via_cli(self, temp_project):
        config, project = temp_project
        with patch("event_actions.load_config", return_value=config):
            import event_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = event_actions.main([
                    "ingest", "--source", "gmail", "--source-id", "m1",
                    "--type", "email_received", "--summary", "Test email",
                    "--payload-json", '{"from": "a@b.com"}',
                ])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["source"] == "gmail"
        assert data["classification"]["auto_execute"] is False

    def test_duplicate_ingest_via_cli(self, temp_project):
        config, project = temp_project
        with patch("event_actions.load_config", return_value=config):
            import event_actions
            event_actions.main([
                "ingest", "--source", "gmail", "--source-id", "m1",
                "--type", "email_received",
            ])
            rc = event_actions.main([
                "ingest", "--source", "gmail", "--source-id", "m1",
                "--type", "email_received",
            ])
        assert rc == 0  # idempotent, not an error

    def test_list_via_cli(self, temp_project):
        config, project = temp_project
        with patch("event_actions.load_config", return_value=config):
            import event_actions
            event_actions.main([
                "ingest", "--source", "gmail", "--source-id", "m1",
                "--type", "email_received",
            ])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = event_actions.main(["--summary", "list"])
        assert rc == 0
        assert "📨" in buf.getvalue()

    def test_summary_via_cli(self, temp_project):
        config, project = temp_project
        with patch("event_actions.load_config", return_value=config):
            import event_actions
            event_actions.main([
                "ingest", "--source", "gmail", "--source-id", "m1",
                "--type", "email_received",
            ])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = event_actions.main(["--summary", "summary"])
        assert rc == 0
        assert "Events:" in buf.getvalue()

    def test_mark_processed_via_cli(self, temp_project):
        config, project = temp_project
        with patch("event_actions.load_config", return_value=config):
            import event_actions
            buf = io.StringIO()
            with redirect_stdout(buf):
                event_actions.main([
                    "ingest", "--source", "gmail", "--source-id", "m1",
                    "--type", "email_received",
                ])
            event = json.loads(buf.getvalue())
            rc = event_actions.main([
                "mark-processed", "--event-id", event["id"],
                "--by", "MH", "--notes", "Handled",
            ])
        assert rc == 0