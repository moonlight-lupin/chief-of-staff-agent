#!/usr/bin/env python3
"""Contract tests for HTML briefing format (v0.5.0 beta).

Tests the render_html() function in briefing_renderer.py and the
event_link field addition to schemas.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _sample_briefing() -> dict:
    """Build a minimal briefing dict for testing."""
    return {
        "operator": "Test Operator",
        "generated_at": "2026-08-24T10:00:00+00:00",
        "summary": {
            "needs_attention": 2,
            "pending_approvals": 1,
            "suggestions": 3,
            "classified_emails": 5,
            "system_warnings": 0,
        },
        "sections": {
            "needs_attention": [
                {"risk": "high", "title": "Overdue invoice INV-001", "detail": "Acme Corp, $5000"},
                {"risk": "medium", "title": "Stale deal", "detail": "Globex, 14 days idle"},
            ],
            "pending_approvals": {
                "mail.send": [{"id": "a1", "summary": "Send proposal to Acme"}],
            },
            "email_organisation": {"classified": 5, "label_suggestions": 2},
            "calendar_deadlines": [
                {
                    "title": "Team standup",
                    "start": "2026-08-24T09:00:00+00:00",
                    "end": "2026-08-24T09:30:00+00:00",
                    "conference_link": "https://meet.google.com/abc-defg-hij",
                    "event_link": "https://calendar.google.com/calendar/event?eid=xyz",
                    "location": "Google Meet",
                },
                {
                    "title": "ACRA filing deadline",
                    "start": "2026-08-25T00:00:00+00:00",
                    "end": "2026-08-25T23:59:59+00:00",
                },
            ],
            "recent_events": [],
            "suggested_next_actions": [
                {"type": "mail.send", "summary": "Follow up with Acme on proposal"},
            ],
            "system_health": {"warnings": 0},
            "knowledge_maintenance": {"wiki_broken_links": 0},
            "bookkeeper": {
                "overdue_ar": [{"id": "INV-001", "client": "Acme", "amount": "5000", "currency": "SGD"}],
                "outstanding_ar_total": {"SGD": "5000"},
            },
            "pipeline": {
                "stale_deals": [{"client_name": "Globex", "stage": "Proposal Sent", "stale_days": 14}],
            },
        },
        "safety": {"mutations": 0},
    }


# ─── render_html exists and returns valid HTML ─────────────────


def test_render_html_returns_string():
    """render_html returns a non-empty string."""
    from briefing_renderer import render_html
    html = render_html(_sample_briefing())
    assert isinstance(html, str)
    assert len(html) > 100  # non-trivial


def test_render_html_contains_doctype():
    """HTML output starts with <!DOCTYPE html>."""
    from briefing_renderer import render_html
    html = render_html(_sample_briefing())
    assert "<!DOCTYPE html>" in html or "<!doctype html>" in html


def test_render_html_contains_operator_name():
    """HTML output contains the operator name."""
    from briefing_renderer import render_html
    html = render_html(_sample_briefing())
    assert "Test Operator" in html


def test_render_html_contains_executive_summary():
    """HTML output contains executive summary counts."""
    from briefing_renderer import render_html
    html = render_html(_sample_briefing())
    assert "2" in html  # needs_attention count


# ─── Links ──────────────────────────────────────────────────────


def test_render_html_contains_calendar_event_link():
    """HTML output contains a link to the calendar event (event_link)."""
    from briefing_renderer import render_html
    html = render_html(_sample_briefing())
    assert "https://calendar.google.com/calendar/event?eid=xyz" in html
    assert "View event" in html


def test_render_html_contains_conference_join_link():
    """HTML output contains a Join meeting button for conference_link."""
    from briefing_renderer import render_html
    html = render_html(_sample_briefing())
    assert "https://meet.google.com/abc-defg-hij" in html
    assert "Join" in html


def test_render_html_event_without_links_still_renders():
    """Calendar events without event_link or conference_link still render."""
    from briefing_renderer import render_html
    briefing = _sample_briefing()
    # Strip links from the second event (ACRA filing)
    briefing["sections"]["calendar_deadlines"][1] = {
        "title": "ACRA filing deadline",
        "start": "2026-08-25T00:00:00+00:00",
    }
    html = render_html(briefing)
    assert "ACRA filing deadline" in html


def test_render_html_email_link():
    """Email items with a link field render as clickable links."""
    from briefing_renderer import render_html
    briefing = _sample_briefing()
    briefing["sections"]["needs_attention"].append({
        "risk": "low",
        "title": "Email from client",
        "detail": "Re: Proposal",
        "link": "https://mail.google.com/mail/u/0/#inbox/abc123",
    })
    html = render_html(briefing)
    assert "https://mail.google.com/mail/u/0/#inbox/abc123" in html


# ─── Collapsible sections ───────────────────────────────────────


def test_render_html_has_collapsible_sections():
    """HTML uses <details> for collapsible sections."""
    from briefing_renderer import render_html
    html = render_html(_sample_briefing())
    assert "<details" in html
    assert "<summary" in html


# ─── Risk badges ────────────────────────────────────────────────


def test_render_html_has_risk_badges():
    """High-risk items have red badge, medium has yellow."""
    from briefing_renderer import render_html
    html = render_html(_sample_briefing())
    assert "#dc2626" in html or "risk-high" in html.lower()
    assert "#f59e0b" in html or "risk-medium" in html.lower()


# ─── Self-contained ─────────────────────────────────────────────


def test_render_html_no_external_deps():
    """HTML must not reference external CSS or JS files."""
    from briefing_renderer import render_html
    html = render_html(_sample_briefing())
    assert "<link rel=\"stylesheet\"" not in html
    assert "<script src=" not in html
    assert "<script>" not in html


def test_render_html_has_inline_css():
    """HTML must contain inline CSS in a <style> tag."""
    from briefing_renderer import render_html
    html = render_html(_sample_briefing())
    assert "<style>" in html


# ─── Empty briefing ─────────────────────────────────────────────


def test_render_html_empty_briefing():
    """render_html handles an empty briefing without error."""
    from briefing_renderer import render_html
    empty = {
        "operator": "Operator",
        "generated_at": "2026-08-24T10:00:00+00:00",
        "summary": {},
        "sections": {},
        "safety": {},
    }
    html = render_html(empty)
    assert isinstance(html, str)
    assert "Operator" in html


# ─── Event schema event_link field ─────────────────────────────


def test_event_schema_has_event_link():
    """schemas.py event shape includes event_link as optional field."""
    from schemas import normalize_event
    event = normalize_event({
        "id": "e1",
        "title": "Test",
        "start": "2026-08-24T09:00:00+00:00",
        "end": "2026-08-24T10:00:00+00:00",
        "event_link": "https://calendar.google.com/event?eid=abc",
    })
    assert event.get("event_link") == "https://calendar.google.com/event?eid=abc"


def test_event_schema_event_link_optional():
    """event_link is optional — event without it still validates."""
    from schemas import normalize_event
    event = normalize_event({
        "id": "e1",
        "title": "Test",
        "start": "2026-08-24T09:00:00+00:00",
        "end": "2026-08-24T10:00:00+00:00",
    })
    assert event.get("event_link") is None or "event_link" not in event