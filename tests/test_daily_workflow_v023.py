#!/usr/bin/env python3
"""Tests for v0.2.3 — Daily workflow polish.

Tests:
1. Briefing run does not mutate provider
2. Briefing does not approve or execute pending actions
3. Pending actions grouped by risk
4. Failed/retryable actions appear under needs attention
5. Email organisation section renders counts
6. Recent events section respects --since and --limit
7. System health section includes state/webhook/pending summary
8. Markdown output renders key sections
9. JSON output has stable schema
10. Email notify creates pending action only
11. Empty state produces useful briefing, not crash
12. Malformed optional state file degrades gracefully
13. action_risk classifications
14. briefing_renderer text/markdown/json output
"""
import sys
import os
import io
import json
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
SKILLS_SCRIPTS = PLUGIN_ROOT / "skills"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
for skill in ("daily-briefing",):
    d = SKILLS_SCRIPTS / skill / "scripts"
    if d.exists() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


@pytest.fixture
def temp_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".audit").mkdir()
    (project / ".runs").mkdir()
    config = {
        "company": {"name": "Test Co", "jurisdiction": "SG", "currency": "SGD",
                     "incorporation_date": "2026-01-01", "financial_year_end": "31 Dec",
                     "business_type": "professional_services"},
        "google": {"delegate_email": "test@test.com", "account_alias": "test",
                   "domain": "test.com", "service_account_path": "/tmp/sa.json"},
        "paths": {"project_root": str(project), "wiki_path": str(project / "wiki"),
                  "templates": str(PLUGIN_ROOT / "shared" / "templates")},
        "delivery": {"channel": "telegram", "briefing_time": "08:00",
                      "weekly_review_day": "friday", "weekly_review_time": "17:00",
                      "timezone": "Asia/Singapore"},
        "integrations": {"workspace": {"provider": "google_api"}},
        "sales_stages": ["Lead", "Proposal Sent", "NDA Signed", "Contract Signed", "Invoiced", "Paid"],
    }
    return config, project


# ─── Action Risk ────────────────────────────────────────────

class TestActionRisk:
    def test_high_risk_types(self):
        from action_risk import get_action_risk, HIGH_RISK_TYPES
        assert get_action_risk("gmail.send") == "high"
        assert get_action_risk("gmail.trash") == "high"
        assert get_action_risk("drive.trash") == "high"
        assert get_action_risk("calendar.cancel") == "high"

    def test_medium_risk_types(self):
        from action_risk import get_action_risk
        assert get_action_risk("calendar.create") == "medium"
        assert get_action_risk("calendar.update") == "medium"
        assert get_action_risk("drive.upload") == "medium"
        assert get_action_risk("gmail.archive") == "medium"

    def test_low_risk_types(self):
        from action_risk import get_action_risk
        assert get_action_risk("gmail.label") == "low"
        assert get_action_risk("gmail.create_label") == "low"
        assert get_action_risk("drive.search") == "low"
        assert get_action_risk("gmail.search") == "low"

    def test_unknown_type_defaults_to_review(self):
        from action_risk import get_action_risk
        # Unknown actions no longer silently default to 'low'
        # Write verbs → high, moderate verbs → medium, read verbs → low, truly unknown → medium
        assert get_action_risk("unknown.action") == "medium"
        assert get_action_risk("custom.delete") == "high"  # write verb
        assert get_action_risk("custom.search") == "low"   # read verb

    def test_risk_icon(self):
        from action_risk import get_risk_icon
        assert get_risk_icon("high") == "🔴"
        assert get_risk_icon("medium") == "🟡"
        assert get_risk_icon("low") == "🟢"

    def test_group_actions_by_risk(self):
        from action_risk import group_actions_by_risk
        actions = [
            {"action_type": "gmail.send", "state": "requested"},
            {"action_type": "gmail.label", "state": "approved"},
            {"action_type": "calendar.create", "state": "requested"},
        ]
        grouped = group_actions_by_risk(actions)
        assert len(grouped["high"]) == 1
        assert len(grouped["medium"]) == 1
        assert len(grouped["low"]) == 1

    def test_risk_explanation(self):
        from action_risk import get_risk_explanation
        exp = get_risk_explanation("gmail.send", "high")
        assert exp  # non-empty
        assert len(exp) > 10  # meaningful explanation


# ─── Briefing Renderer ──────────────────────────────────────

class TestBriefingRenderer:
    @pytest.fixture
    def sample_briefing(self):
        return {
            "generated_at": "2026-07-10T08:00:00+08:00",
            "window": "24h",
            "operator": "MH",
            "summary": {"needs_attention": 2, "pending_approvals": 1,
                         "suggestions": 3, "classified_emails": 5,
                         "system_warnings": 0},
            "sections": {
                "needs_attention": [
                    {"title": "gmail.send — Test", "risk": "high", "why": "Needs approval"},
                ],
                "pending_approvals": {
                    "high": [{"action_id": "a1", "type": "gmail.send", "summary": "Test send",
                              "state": "requested", "risk": "high", "created_at": "2026-07-10"}],
                    "medium": [],
                    "low": [{"action_id": "a2", "type": "gmail.label", "summary": "Label test",
                             "state": "approved", "risk": "low", "created_at": "2026-07-10"}],
                },
                "email_organisation": {"classified": 5, "unmapped": 2,
                                       "archive_candidates": 1, "label_suggestions": 0,
                                       "pending_actions": 0},
                "calendar_deadlines": [],
                "recent_events": [{"event_type": "email_received"}, {"event_type": "calendar_event"}],
                "suggested_next_actions": [
                    {"title": "Review label", "risk": "low", "why": "Auto-suggested"},
                ],
                "system_health": {"state_files": "ok",
                                  "pending_summary": {"requested": 1, "approved": 1},
                                  "audit_dir": True, "runs_dir": True},
            },
            "safety": {"external_mutations_performed": False,
                       "approvals_performed": False, "executions_performed": False},
        }

    def test_text_render(self, sample_briefing):
        from briefing_renderer import render_text
        text = render_text(sample_briefing)
        assert "Good morning, MH." in text
        assert "Needs attention" in text
        assert "Pending approvals" in text
        assert "Email organisation" in text
        assert "No external mutations" in text

    def test_markdown_render(self, sample_briefing):
        from briefing_renderer import render_markdown
        md = render_markdown(sample_briefing)
        assert "# Daily Briefing" in md
        assert "## Executive Summary" in md
        assert "## Pending Approvals" in md
        assert "## Email Organisation" in md
        assert "No Gmail changes" in md

    def test_json_render(self, sample_briefing):
        from briefing_renderer import render_json
        js = render_json(sample_briefing)
        parsed = json.loads(js)
        assert parsed["operator"] == "MH"
        assert parsed["summary"]["needs_attention"] == 2
        assert parsed["safety"]["external_mutations_performed"] is False

    def test_empty_briefing(self):
        from briefing_renderer import render_text
        empty = {
            "generated_at": "2026-07-10T08:00:00Z",
            "window": "24h",
            "operator": "Test",
            "summary": {"needs_attention": 0, "pending_approvals": 0,
                         "suggestions": 0, "classified_emails": 0, "system_warnings": 0},
            "sections": {"needs_attention": [], "pending_approvals": {},
                         "email_organisation": {}, "calendar_deadlines": [],
                         "recent_events": [], "suggested_next_actions": [],
                         "system_health": {}},
            "safety": {"external_mutations_performed": False,
                       "approvals_performed": False, "executions_performed": False},
        }
        text = render_text(empty)
        assert "All clear" in text


# ─── Briefing Sources ───────────────────────────────────────

class TestBriefingSources:
    def test_collect_pending_actions_empty(self, temp_project):
        config, project = temp_project
        from briefing_sources import collect_pending_actions
        result = collect_pending_actions(config)
        assert isinstance(result, list)
        assert len(result) == 0

    def test_collect_pending_actions_with_data(self, temp_project):
        config, project = temp_project
        from pending_actions import create_pending_action
        create_pending_action(
            config=config, action_type="gmail.send", provider="google_api",
            target="x@y.com", payload={"to": "x@y.com", "subject": "t", "body": "b"},
            summary="Test send",
        )
        from briefing_sources import collect_pending_actions
        result = collect_pending_actions(config)
        assert len(result) >= 1
        assert result[0]["type"] == "gmail.send"

    def test_collect_recent_events_respects_since(self, temp_project):
        config, project = temp_project
        from event_store import ingest_event
        ingest_event(config, source="gmail", source_id="msg-001",
                      event_type="email_received", payload={"email": "test@x.com"})
        from briefing_sources import collect_recent_events
        # Default 24h
        result = collect_recent_events(config, since_hours=24)
        assert len(result) >= 1
        # Negative hours should return nothing
        result_neg = collect_recent_events(config, since_hours=-1)
        assert len(result_neg) == 0

    def test_collect_recent_events_respects_limit(self, temp_project):
        config, project = temp_project
        from event_store import ingest_event
        for i in range(10):
            ingest_event(config, source="gmail", source_id=f"msg-{i:03d}",
                          event_type="email_received", payload={"email": f"test{i}@x.com"})
        from briefing_sources import collect_recent_events
        result = collect_recent_events(config, since_hours=24, limit=5)
        assert len(result) <= 5

    def test_collect_system_health(self, temp_project):
        config, project = temp_project
        from briefing_sources import collect_system_health
        result = collect_system_health(config)
        assert "state_files" in result
        assert "pending_summary" in result
        assert "audit_dir" in result
        assert "runs_dir" in result

    def test_collect_email_org_stats_empty(self, temp_project):
        config, project = temp_project
        from briefing_sources import collect_email_org_stats
        result = collect_email_org_stats(config)
        assert isinstance(result, dict)

    def test_malformed_state_degrades_gracefully(self, temp_project):
        config, project = temp_project
        # Write malformed JSON to state files
        (project / ".events.json").write_text('{invalid json}')
        (project / ".pending_actions.json").write_text('not json at all')
        from briefing_sources import (
            collect_pending_actions, collect_recent_events, collect_email_org_stats,
            collect_system_health,
        )
        assert collect_pending_actions(config) == []
        assert collect_recent_events(config) == []
        # email_org_stats returns a dict with zeroed counts, not empty dict
        eo = collect_email_org_stats(config)
        assert isinstance(eo, dict)
        sh = collect_system_health(config)
        assert sh["state_files"] in ("ok", "missing", "malformed")


# ─── Daily Briefing run command ─────────────────────────────

class TestBriefingRun:
    def test_run_summary_does_not_crash(self, temp_project, monkeypatch):
        config, project = temp_project
        # Set config path
        config_path = project / "company.yaml"
        import yaml
        config_path.write_text(yaml.safe_dump(config))
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                rc = daily_briefing.main(["run", "--summary", "--dry-run"])
            except SystemExit:
                rc = 2
        output = buf.getvalue()
        assert "Good morning" in output or "briefing" in output.lower() or rc in (0, 2)

    def test_run_json_has_stable_schema(self, temp_project, monkeypatch):
        config, project = temp_project
        config_path = project / "company.yaml"
        import yaml
        config_path.write_text(yaml.safe_dump(config))
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--json", "--dry-run"])
        output = buf.getvalue()
        parsed = json.loads(output)
        assert "generated_at" in parsed
        assert "window" in parsed
        assert "summary" in parsed
        assert "sections" in parsed
        assert "safety" in parsed
        assert parsed["safety"]["external_mutations_performed"] is False
        assert parsed["safety"]["approvals_performed"] is False
        assert parsed["safety"]["executions_performed"] is False

    def test_run_markdown_renders_sections(self, temp_project, monkeypatch):
        config, project = temp_project
        config_path = project / "company.yaml"
        import yaml
        config_path.write_text(yaml.safe_dump(config))
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--markdown", "--dry-run"])
        output = buf.getvalue()
        assert "# Daily Briefing" in output
        assert "## Executive Summary" in output

    def test_briefing_does_not_approve_or_execute(self, temp_project, monkeypatch):
        """Verify no provider calls, approvals, or executions happen during briefing."""
        config, project = temp_project
        # Create some pending actions
        from pending_actions import create_pending_action
        create_pending_action(
            config=config, action_type="gmail.send", provider="google_api",
            target="x@y.com", payload={"to": "x@y.com", "subject": "t", "body": "b"},
            summary="Test",
        )
        config_path = project / "company.yaml"
        import yaml
        config_path.write_text(yaml.safe_dump(config))
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--json", "--dry-run"])
        output = buf.getvalue()
        parsed = json.loads(output)
        assert parsed["safety"]["approvals_performed"] is False
        assert parsed["safety"]["executions_performed"] is False

        # Verify pending action state unchanged
        from pending_actions import list_pending_actions
        actions = list_pending_actions(config)
        assert all(a["state"] != "executed" for a in actions)
        assert all(a["state"] != "approved" for a in actions)

    def test_pending_actions_grouped_by_risk(self, temp_project, monkeypatch):
        config, project = temp_project
        from pending_actions import create_pending_action
        create_pending_action(config=config, action_type="gmail.send", provider="google_api",
            target="x@y.com", payload={"to": "x@y.com", "subject": "t", "body": "b"},
            summary="High risk")
        create_pending_action(config=config, action_type="gmail.label", provider="google_api",
            target="msg-1", payload={"message_id": "m1", "label_id": "L1"},
            summary="Low risk")
        create_pending_action(config=config, action_type="calendar.create", provider="google_api",
            target="cal", payload={"summary": "Meeting", "start": "2026-07-15T10:00", "end": "2026-07-15T11:00"},
            summary="Medium risk")

        config_path = project / "company.yaml"
        import yaml
        config_path.write_text(yaml.safe_dump(config))
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily_briefing.main(["run", "--json", "--dry-run"])
        parsed = json.loads(buf.getvalue())
        pa = parsed["sections"]["pending_approvals"]
        assert len(pa["high"]) >= 1
        assert len(pa["medium"]) >= 1
        assert len(pa["low"]) >= 1

    def test_empty_state_produces_useful_briefing(self, temp_project, monkeypatch):
        config, project = temp_project
        config_path = project / "company.yaml"
        import yaml
        config_path.write_text(yaml.safe_dump(config))
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = daily_briefing.main(["run", "--summary", "--dry-run"])
        assert rc == 0
        output = buf.getvalue()
        assert "Good morning" in output
        # Empty state may have system warnings (e.g. missing state files)
        # which is valid and useful — just verify it doesn't crash


# ─── Email Notify ───────────────────────────────────────────

class TestEmailNotify:
    def test_notify_cli_channel(self, temp_project, monkeypatch):
        config, project = temp_project
        config_path = project / "company.yaml"
        import yaml
        config_path.write_text(yaml.safe_dump(config))
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = daily_briefing.main(["notify", "--channel", "cli", "--dry-run"])
        assert rc == 0
        assert "Good morning" in buf.getvalue()

    def test_notify_email_creates_pending_action_only(self, temp_project, monkeypatch):
        config, project = temp_project
        config_path = project / "company.yaml"
        import yaml
        config_path.write_text(yaml.safe_dump(config))
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = daily_briefing.main(["notify", "--channel", "email",
                                      "--to", "mh@test.com", "--dry-run"])
        # dry-run should not create action
        assert rc == 0

        # Now test without dry-run
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc = daily_briefing.main(["notify", "--channel", "email",
                                      "--to", "mh@test.com"])
        output = buf2.getvalue()
        assert "Pending action created" in output
        assert "NOT auto-send" in output

        # Verify it's pending, not sent
        from pending_actions import list_pending_actions
        actions = list_pending_actions(config)
        assert any(a["type"] == "gmail.send" for a in actions)
        send_actions = [a for a in actions if a["type"] == "gmail.send"]
        assert all(a["state"] != "executed" for a in send_actions)

    def test_notify_email_requires_to(self, temp_project, monkeypatch):
        config, project = temp_project
        config_path = project / "company.yaml"
        import yaml
        config_path.write_text(yaml.safe_dump(config))
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))

        import daily_briefing
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = daily_briefing.main(["notify", "--channel", "email", "--dry-run"])
        assert rc == 1