#!/usr/bin/env python3
"""Regression tests for v0.5.0 review fixes (FIX_BRIEF_v050)."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
DAILY = PLUGIN_ROOT / "skills" / "daily-briefing" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(DAILY) not in sys.path:
    sys.path.insert(0, str(DAILY))


def _cfg(root: Path, **delivery: object) -> dict:
    data: dict = {
        "company": {
            "name": "Test Operator",
            "jurisdiction": "SG",
            "currency": "SGD",
            "incorporation_date": "2026-01-01",
            "financial_year_end": "31 Dec",
            "business_type": "professional_services",
        },
        "paths": {"project_root": str(root)},
        "sales_stages": ["Lead", "Proposal Sent", "Paid"],
        "delivery": {"default_format": "text", **delivery},
    }
    (root / "company.yaml").write_text(yaml.safe_dump(data))
    return data


# ─── B2 / B3 / m2 / M4: HTML renderer ─────────────────────────────────────


def test_link_rejects_javascript_scheme():
    from briefing_renderer import _link

    assert _link("javascript:alert(1)", "Click") == ""
    assert _link("JAVASCRIPT:alert(1)", "Click") == ""
    assert _link("data:text/html,hi", "Click") == ""
    html = _link("https://calendar.google.com/event?eid=abc", "View event")
    assert "https://calendar.google.com/event?eid=abc" in html
    assert "View event" in html


def test_risk_badge_escapes_unknown_label():
    from briefing_renderer import _risk_badge

    html = _risk_badge("<img src=x onerror=alert(1)>")
    assert "<img" not in html
    assert "&lt;" in html
    assert _risk_badge("high") == '<span class="badge badge-high">High</span>'


def test_esc_preserves_zero():
    from briefing_renderer import _esc, _html_table

    assert _esc(0) == "0"
    assert _esc(0.0) == "0.0"
    assert _esc(False) == "False"
    assert _esc(None) == ""
    assert _esc("") == ""
    table = _html_table([{"amount": 0}], [("amount", "Amount")])
    assert "<td>0</td>" in table


def test_html_pending_approvals_uses_action_type_not_risk_key():
    from briefing_renderer import render_html

    briefing = {
        "operator": "Op",
        "generated_at": "2026-08-24T10:00:00+00:00",
        "summary": {"pending_approvals": 1},
        "sections": {
            "pending_approvals": {
                "high": [
                    {
                        "action_id": "a1",
                        "type": "mail.send",
                        "summary": "Send proposal to Acme",
                        "state": "requested",
                    }
                ],
                "medium": [],
                "low": [],
            }
        },
        "safety": {},
    }
    html = render_html(briefing)
    assert "mail.send" in html
    assert "Send proposal to Acme" in html
    assert "<strong>high</strong>" not in html
    assert "badge-high" in html


# ─── B1 / M1: CLI --html / --output / default_format ──────────────────────


def test_run_html_flag_prints_html(tmp_path):
    import daily_briefing

    _cfg(tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = daily_briefing.main([
            "run", "--config", str(tmp_path / "company.yaml"),
            "--html", "--dry-run",
        ])
    assert rc == 0
    out = buf.getvalue()
    assert "<!DOCTYPE html>" in out or "<!doctype html>" in out
    assert "Test Operator" in out


def test_run_html_output_writes_file(tmp_path):
    import daily_briefing

    _cfg(tmp_path)
    dest = tmp_path / "out" / "briefing.html"
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = daily_briefing.main([
            "run", "--config", str(tmp_path / "company.yaml"),
            "--html", "--output", str(dest), "--dry-run",
        ])
    assert rc == 0
    assert dest.exists()
    text = dest.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in text or "<!doctype html>" in text
    assert buf.getvalue() == ""


def test_run_format_flags_are_mutually_exclusive(tmp_path):
    import daily_briefing

    _cfg(tmp_path)
    with pytest.raises(SystemExit):
        daily_briefing.build_parser().parse_args(["run", "--html", "--json"])


def test_default_format_html_writes_media_attachment(tmp_path):
    import daily_briefing

    _cfg(tmp_path, default_format="html")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = daily_briefing.main([
            "run", "--config", str(tmp_path / "company.yaml"), "--dry-run",
        ])
    assert rc == 0
    out = buf.getvalue().strip()
    assert out.startswith("MEDIA:")
    attached = Path(out.split("MEDIA:", 1)[1].strip())
    assert attached.exists()
    body = attached.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in body or "<!doctype html>" in body


def test_cli_json_overrides_default_format_html(tmp_path):
    import daily_briefing

    _cfg(tmp_path, default_format="html")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = daily_briefing.main([
            "run", "--config", str(tmp_path / "company.yaml"),
            "--json", "--dry-run",
        ])
    assert rc == 0
    parsed = json.loads(buf.getvalue())
    assert "sections" in parsed


# ─── B4: calendar summary fields survive into HTML ────────────────────────


def test_calendar_summary_html_contains_event_link(tmp_path):
    from briefing_renderer import render
    from daily_briefing import _build_structured_briefing

    _cfg(tmp_path)
    envelope = {
        "events": [
            {
                "id": "evt-1",
                "title": "Team standup",
                "start": "2026-08-24T09:00:00+00:00",
                "end": "2026-08-24T09:30:00+00:00",
                "event_link": "https://calendar.google.com/calendar/event?eid=xyz",
                "conference_link": "https://meet.google.com/abc-defg-hij",
                "location": "Google Meet",
            }
        ]
    }
    briefing = _build_structured_briefing(
        str(tmp_path / "company.yaml"),
        workspace_input=envelope,
    )
    cal = briefing["sections"]["calendar_deadlines"]
    assert cal, "calendar_deadlines should be populated from the envelope"
    assert cal[0].get("event_link") == "https://calendar.google.com/calendar/event?eid=xyz"
    assert cal[0].get("start")
    assert cal[0].get("end")
    html = render(briefing, "html")
    assert "https://calendar.google.com/calendar/event?eid=xyz" in html
    assert "View event" in html
    assert "https://meet.google.com/abc-defg-hij" in html


# ─── M2: YAML migrated aside; SQLite is the read path ─────────────────────


def test_yaml_renamed_after_migration_and_sqlite_is_source(tmp_path):
    from state_db import StateDB, load_store

    yaml_path = tmp_path / "pipeline.yaml"
    yaml_path.write_text(yaml.safe_dump({
        "deals": [{"id": "deal-seed", "client_name": "Acme", "stage": "Lead",
                   "value": 1, "currency": "SGD", "created": "2026-01-01",
                   "last_activity": "2026-01-01"}],
    }))
    db = StateDB(_cfg(tmp_path))
    loaded = db.get_kv("pipeline")
    db.close()
    assert loaded["deals"][0]["id"] == "deal-seed"
    assert yaml_path.with_name("pipeline.yaml.migrated").exists()

    # Stale YAML written beside SQLite must not be read as source of truth.
    yaml_path.write_text(yaml.safe_dump({"deals": [{"id": "stale-yaml"}]}))
    again = load_store("pipeline", _cfg(tmp_path), validate=False)
    assert again["deals"][0]["id"] == "deal-seed"


def test_load_pipeline_store_strict_false_uses_sqlite(tmp_path):
    sys.path.insert(0, str(PLUGIN_ROOT / "skills" / "pipeline-manager" / "scripts"))
    import pipeline
    from state_db import save_store_atomic

    config = _cfg(tmp_path)
    save_store_atomic(
        "pipeline",
        {"deals": [{"id": "from-sqlite", "client_name": "Acme", "stage": "Lead",
                    "value": 1, "currency": "SGD", "created": "2026-01-01",
                    "last_activity": "2026-01-01"}]},
        config=config,
        _fill_defaults=True,
    )
    (tmp_path / "pipeline.yaml").write_text(yaml.safe_dump({"deals": [{"id": "from-yaml"}]}))
    data = pipeline.load_pipeline_store(config, strict=False)
    assert data["deals"][0]["id"] == "from-sqlite"


# ─── m1: lease renewal token ──────────────────────────────────────────────


def test_renew_delivery_rejects_none_when_token_stored(tmp_path):
    from state_db import StateDB

    db = StateDB(_cfg(tmp_path))
    reserved = db.reserve_delivery("del-token")
    assert reserved[0] is True
    assert reserved.lease_token
    assert db.renew_delivery("del-token", lease_token=None) is False
    assert db.renew_delivery("del-token", lease_token=reserved.lease_token) is True
    db.close()


def test_renew_delivery_allows_tokenless_legacy_row(tmp_path):
    from state_db import StateDB

    db = StateDB(_cfg(tmp_path))
    db.conn.execute(
        "INSERT INTO webhook_replay (delivery_id, state, ts, lease_token) VALUES (?,?,?,?)",
        ("del-legacy", "processing", 1.0, None),
    )
    db.conn.commit()
    assert db.renew_delivery("del-legacy", lease_token=None) is True
    db.close()
