#!/usr/bin/env python3
"""Tests for v0.1.22 — event state fix and polling connectors.

Verifies:
- ingest_event() returns state == "classified" (not "received")
- mark_surfaced() works immediately after ingest
- mark_processed() works from classified state
- Polling Gmail/Calendar/Drive ingests events
- Polling same source twice does not duplicate (dedupe)
- Polling never calls provider write methods
- poll --all works for all three sources
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
    """Mock workspace client with read methods."""
    mock = MagicMock()
    mock.provider_name = "google_api"
    mock.supports.side_effect = lambda action: True
    mock.gmail_search.return_value = [
        {"id": "msg001", "subject": "NDA Review", "from": "client@x.com",
         "snippet": "Please review the NDA", "labelIds": ["INBOX"]},
        {"id": "msg002", "subject": "Invoice", "from": "billing@x.com",
         "snippet": "Invoice attached", "labelIds": ["INBOX", "IMPORTANT"]},
    ]
    mock.calendar_list.return_value = [
        {"id": "evt001", "summary": "Team Sync", "status": "confirmed",
         "start": "2026-07-10T10:00:00Z", "end": "2026-07-10T11:00:00Z"},
        {"id": "evt002", "summary": "Cancelled Meeting", "status": "cancelled",
         "start": "2026-07-10T14:00:00Z", "end": "2026-07-10T15:00:00Z"},
    ]
    mock.drive_search.return_value = [
        {"id": "file001", "name": "NDA_Template.docx", "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "webViewLink": "https://drive.google.com/file/d/file001/view"},
        {"id": "file002", "name": "Budget.xlsx", "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "sharedWithMeTime": "2026-07-09T10:00:00Z",
         "webViewLink": "https://drive.google.com/file/d/file002/view"},
    ]
    return mock


# ─── State Fix ────────────────────────────────────────────────

class TestEventStateFix:
    """Verify the event state machine fix."""

    def test_ingest_returns_classified_state(self, temp_project):
        from event_store import ingest_event
        config, project = temp_project
        event = ingest_event(config, "gmail", "m1", "email_received", {})
        assert event["state"] == "classified"

    def test_mark_surfaced_works_after_ingest(self, temp_project):
        """mark_surfaced should work immediately after ingest (no 'received' gap)."""
        from event_store import ingest_event, mark_surfaced, get_event
        config, project = temp_project
        event = ingest_event(config, "gmail", "m1", "email_received", {})
        surfaced = mark_surfaced(config, event["id"])
        assert surfaced is not None
        assert surfaced["state"] == "surfaced"
        assert surfaced["surfaced_at"] is not None

    def test_mark_processed_from_classified(self, temp_project):
        """mark_processed should work from 'classified' state."""
        from event_store import ingest_event, mark_processed
        config, project = temp_project
        event = ingest_event(config, "gmail", "m1", "email_received", {})
        result = mark_processed(config, event["id"], processed_by="MH")
        assert result is not None
        assert result["state"] == "processed"

    def test_full_lifecycle_classified_to_processed(self, temp_project):
        """Full lifecycle: ingest → classified → surfaced → processed."""
        from event_store import ingest_event, mark_surfaced, mark_processed, get_event
        config, project = temp_project
        event = ingest_event(config, "gmail", "m1", "email_received", {})
        assert event["state"] == "classified"
        mark_surfaced(config, event["id"])
        loaded = get_event(config, event["id"])
        assert loaded["state"] == "surfaced"
        mark_processed(config, event["id"], notes="Done")
        loaded = get_event(config, event["id"])
        assert loaded["state"] == "processed"


# ─── Polling: Gmail ───────────────────────────────────────────

class TestPollGmail:
    """Test Gmail polling ingestion."""

    def test_poll_gmail_ingests_events(self, temp_project, mock_client):
        from poll_events import poll_gmail
        config, project = temp_project
        with patch("poll_events.get_client", return_value=mock_client):
            result = poll_gmail(config, max_results=10)
        assert result["polled"] == 2
        assert result["ingested"] == 2
        assert result["duplicates"] == 0
        assert result["errors"] == 0

    def test_poll_gmail_dedupe_on_second_poll(self, temp_project, mock_client):
        """Polling twice should not duplicate events."""
        from poll_events import poll_gmail
        from event_store import list_events
        config, project = temp_project
        with patch("poll_events.get_client", return_value=mock_client):
            first = poll_gmail(config, max_results=10)
            second = poll_gmail(config, max_results=10)
        assert first["ingested"] == 2
        assert second["ingested"] == 0
        assert second["duplicates"] == 2
        events = list_events(config, source="gmail")
        assert len(events) == 2  # still only 2, not 4

    def test_poll_gmail_classifies_urgent(self, temp_project, mock_client):
        """Email with IMPORTANT label should be classified as email_urgent."""
        from poll_events import poll_gmail
        from event_store import list_events
        config, project = temp_project
        with patch("poll_events.get_client", return_value=mock_client):
            poll_gmail(config, max_results=10)
        events = list_events(config, source="gmail")
        urgent = [e for e in events if e["classification"]["category"] == "email_urgent"]
        assert len(urgent) == 1  # msg002 has IMPORTANT label

    def test_poll_gmail_never_calls_writes(self, temp_project, mock_client):
        """Polling must never call provider write methods."""
        from poll_events import poll_gmail
        config, project = temp_project
        with patch("poll_events.get_client", return_value=mock_client):
            poll_gmail(config, max_results=10)
        mock_client.gmail_send.assert_not_called()
        mock_client.gmail_create_draft.assert_not_called()


# ─── Polling: Calendar ────────────────────────────────────────

class TestPollCalendar:
    """Test Calendar polling ingestion."""

    def test_poll_calendar_ingests_events(self, temp_project, mock_client):
        from poll_events import poll_calendar
        config, project = temp_project
        with patch("poll_events.get_client", return_value=mock_client):
            result = poll_calendar(config, days=1)
        assert result["polled"] == 2
        assert result["ingested"] == 2
        assert result["errors"] == 0

    def test_poll_calendar_dedupe(self, temp_project, mock_client):
        from poll_events import poll_calendar
        from event_store import list_events
        config, project = temp_project
        with patch("poll_events.get_client", return_value=mock_client):
            poll_calendar(config, days=1)
            poll_calendar(config, days=1)
        events = list_events(config, source="calendar")
        assert len(events) == 2  # not 4

    def test_poll_calendar_classifies_cancelled(self, temp_project, mock_client):
        from poll_events import poll_calendar
        from event_store import list_events
        config, project = temp_project
        with patch("poll_events.get_client", return_value=mock_client):
            poll_calendar(config, days=1)
        events = list_events(config, source="calendar")
        cancelled = [e for e in events if e["classification"]["category"] == "calendar_cancelled"]
        assert len(cancelled) == 1  # evt002 has status=cancelled

    def test_poll_calendar_never_calls_writes(self, temp_project, mock_client):
        from poll_events import poll_calendar
        config, project = temp_project
        with patch("poll_events.get_client", return_value=mock_client):
            poll_calendar(config, days=1)
        mock_client.calendar_create.assert_not_called()
        mock_client.calendar_update.assert_not_called()
        mock_client.calendar_cancel.assert_not_called()


# ─── Polling: Drive ───────────────────────────────────────────

class TestPollDrive:
    """Test Drive polling ingestion."""

    def test_poll_drive_ingests_events(self, temp_project, mock_client):
        from poll_events import poll_drive
        config, project = temp_project
        with patch("poll_events.get_client", return_value=mock_client):
            result = poll_drive(config, max_results=10)
        assert result["polled"] == 2
        assert result["ingested"] == 2
        assert result["errors"] == 0

    def test_poll_drive_dedupe(self, temp_project, mock_client):
        from poll_events import poll_drive
        from event_store import list_events
        config, project = temp_project
        with patch("poll_events.get_client", return_value=mock_client):
            poll_drive(config, max_results=10)
            poll_drive(config, max_results=10)
        events = list_events(config, source="drive")
        assert len(events) == 2  # not 4

    def test_poll_drive_never_calls_writes(self, temp_project, mock_client):
        from poll_events import poll_drive
        config, project = temp_project
        with patch("poll_events.get_client", return_value=mock_client):
            poll_drive(config, max_results=10)
        mock_client.drive_upload.assert_not_called()
        mock_client.drive_trash.assert_not_called()


# ─── Polling: All Sources ─────────────────────────────────────

class TestPollAll:
    """Test polling all sources at once."""

    def test_poll_all_sources(self, temp_project, mock_client):
        from poll_events import poll_gmail, poll_calendar, poll_drive
        config, project = temp_project
        with patch("poll_events.get_client", return_value=mock_client):
            gmail_res = poll_gmail(config, max_results=10)
            cal_res = poll_calendar(config, days=1)
            drive_res = poll_drive(config, max_results=10)
        assert gmail_res["ingested"] == 2
        assert cal_res["ingested"] == 2
        assert drive_res["ingested"] == 2
        from event_store import list_events
        all_events = list_events(config)
        assert len(all_events) == 6

    def test_poll_all_dedupe_on_second_run(self, temp_project, mock_client):
        from poll_events import poll_gmail, poll_calendar, poll_drive
        from event_store import list_events
        config, project = temp_project
        with patch("poll_events.get_client", return_value=mock_client):
            # First poll
            poll_gmail(config)
            poll_calendar(config)
            poll_drive(config)
            # Second poll
            poll_gmail(config)
            poll_calendar(config)
            poll_drive(config)
        all_events = list_events(config)
        assert len(all_events) == 6  # still 6, not 12

    def test_poll_cli_all(self, temp_project, mock_client):
        config, project = temp_project
        with patch("poll_events.load_config", return_value=config), \
             patch("poll_events.get_client", return_value=mock_client):
            import poll_events
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = poll_events.main(["--summary", "all", "--max", "10", "--days", "1"])
        assert rc == 0
        out = buf.getvalue()
        assert "gmail:" in out
        assert "calendar:" in out
        assert "drive:" in out

    def test_poll_cli_gmail_only(self, temp_project, mock_client):
        config, project = temp_project
        with patch("poll_events.load_config", return_value=config), \
             patch("poll_events.get_client", return_value=mock_client):
            import poll_events
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = poll_events.main(["--summary", "gmail"])
        assert rc == 0
        out = buf.getvalue()
        assert "gmail:" in out
        assert "calendar:" not in out

    def test_poll_cli_drive_json(self, temp_project, mock_client):
        config, project = temp_project
        with patch("poll_events.load_config", return_value=config), \
             patch("poll_events.get_client", return_value=mock_client):
            import poll_events
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = poll_events.main(["drive", "--max", "5"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert "drive" in data
        assert data["drive"]["ingested"] == 2

    def test_poll_handles_provider_error(self, temp_project):
        """Polling should handle provider errors gracefully."""
        from poll_events import poll_gmail
        config, project = temp_project
        mock_bad = MagicMock()
        mock_bad.provider_name = "google_api"
        mock_bad.gmail_search.side_effect = Exception("Auth failed")
        with patch("poll_events.get_client", return_value=mock_bad):
            result = poll_gmail(config, max_results=10)
        assert result["errors"] == 1
        assert result["ingested"] == 0
        assert "Auth failed" in result["details"][0]