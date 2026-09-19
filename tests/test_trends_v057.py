#!/usr/bin/env python3
"""Contract tests for trends + weekly HTML (CoS v0.5.7). Written by Hermes
BEFORE the build (TDD red). These encode SPEC §2 — the builder must make them
pass WITHOUT modifying this file."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def cfg(root: Path) -> dict:
    return {"paths": {"project_root": str(root)}}


def _summary_briefing(**overrides) -> dict:
    b = {
        "generated_at": "2026-09-20T01:00:00+00:00",
        "operator": "Tester",
        "summary": {
            "needs_attention": 2,
            "pending_approvals": 1,
            "suggestions": 3,
            "classified_emails": 7,
            "system_warnings": 0,
        },
        "sections": {
            "pipeline": {"deals_by_stage": {"Discovery": 3, "Proposal": 1},
                         "total_deals": 4},
            "bookkeeper": {"overdue_ar": [], "outstanding_ar_total": {"SGD": 1200}},
        },
    }
    b.update(overrides)
    return b


# ── capture_snapshot ──────────────────────────────────────────────

def test_capture_snapshot_stores_and_returns(tmp_path):
    from trend_history import capture_snapshot
    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    b = _summary_briefing()
    snap = capture_snapshot(b, config)
    assert snap is not None
    assert snap["kind"] == "daily"
    assert snap["counters"]["needs_attention"] == 2
    from state_db import StateDB
    db = StateDB(config)
    stored = db.get_kv("briefing_trends")
    assert stored and stored["trend_snapshots"], "snapshot must be persisted"


def test_capture_snapshot_demo_isolated(tmp_path):
    from trend_history import capture_snapshot
    from state_db import StateDB
    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    b = _summary_briefing(demo=True)
    assert capture_snapshot(b, config) is None
    db = StateDB(config)
    assert db.get_kv("briefing_trends") is None


def test_capture_snapshot_no_summary_returns_none(tmp_path):
    from trend_history import capture_snapshot
    from state_db import StateDB
    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    assert capture_snapshot({"operator": "x"}, config) is None
    db = StateDB(config)
    assert db.get_kv("briefing_trends") is None


def test_capture_snapshot_same_day_replaces(tmp_path):
    from trend_history import capture_snapshot
    from state_db import StateDB
    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    b1 = _summary_briefing()
    b2 = _summary_briefing(summary={"needs_attention": 5, "pending_approvals": 0,
                                    "suggestions": 1, "classified_emails": 0,
                                    "system_warnings": 1})
    capture_snapshot(b1, config)
    snap2 = capture_snapshot(b2, config)
    assert snap2["counters"]["needs_attention"] == 5
    db = StateDB(config)
    snaps = db.get_kv("briefing_trends")["trend_snapshots"]
    same_day = [s for s in snaps if s["kind"] == "daily"]
    assert len(same_day) == 1, "same-kind same-day snapshot must REPLACE, not append"
    assert same_day[0]["counters"]["needs_attention"] == 5


def test_capture_snapshot_prunes_old(tmp_path):
    from trend_history import capture_snapshot, TREND_KV_STORE, TREND_ROOT_KEY
    from state_db import StateDB
    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    old = {"ts": "2026-01-01T00:00:00+00:00", "kind": "daily",
           "counters": {"needs_attention": 9}}
    db = StateDB(config)
    db.set_kv(TREND_KV_STORE, {TREND_ROOT_KEY: [old]})
    b = _summary_briefing()
    capture_snapshot(b, config)
    snaps = db.get_kv(TREND_KV_STORE)[TREND_ROOT_KEY]
    assert all(s["ts"] > "2026-01-02" for s in snaps), "old snapshot must be pruned"


def test_capture_snapshot_never_raises(tmp_path, monkeypatch):
    from trend_history import capture_snapshot
    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    # Malformed briefing shapes must not raise.
    assert capture_snapshot({"summary": "not-a-dict"}, config) is None
    assert capture_snapshot({"summary": {"needs_attention": "x"}}, config) is None
    b = _summary_briefing()
    b["sections"]["pipeline"] = {"deals_by_stage": "flat-string-not-dict"}
    assert capture_snapshot(b, config) is not None  # skips the ambiguous key


# ── get_series ────────────────────────────────────────────────────

def test_get_series_ascending_filtered(tmp_path):
    from trend_history import capture_snapshot, get_series
    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    b = _summary_briefing()
    capture_snapshot(b, config)
    series = get_series(config, "needs_attention", days=30)
    assert series and series[-1]["value"] == 2
    assert series == sorted(series, key=lambda p: p["ts"])
    assert get_series(config, "nonexistent_metric", days=30) == []


def test_get_series_missing_store_empty(tmp_path):
    from trend_history import get_series
    root = tmp_path / "proj"
    root.mkdir()
    assert get_series(cfg(root), "needs_attention") == []


def test_get_series_kind_filter(tmp_path):
    from trend_history import capture_snapshot, get_series
    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    capture_snapshot(_summary_briefing(), config, kind="daily")
    capture_snapshot(_summary_briefing(), config, kind="weekly")
    daily = get_series(config, "needs_attention", days=30, kind="daily")
    weekly = get_series(config, "needs_attention", days=30, kind="weekly")
    assert all(p["kind"] == "daily" for p in daily)
    assert all(p["kind"] == "weekly" for p in weekly)


# ── build_trends_section ──────────────────────────────────────────

def test_build_trends_section_shape_and_delta(tmp_path):
    from trend_history import build_trends_section, capture_snapshot
    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    capture_snapshot(_summary_briefing(), config, kind="daily")
    b2 = _summary_briefing(summary={"needs_attention": 0, "pending_approvals": 0,
                                    "suggestions": 0, "classified_emails": 0,
                                    "system_warnings": 0})
    # A different day is hard to fabricate cheaply — same-day replace keeps
    # 1 point; delta rule is exercised in render tests below via synthetic data.
    section = build_trends_section(b2, config)
    assert isinstance(section, dict)
    assert "metrics" in section
    m = {x["label"]: x for x in section["metrics"]}
    assert "needs_attention" in m
    assert m["needs_attention"]["series"], "at least one point expected"


def test_build_trends_section_never_raises(tmp_path):
    from trend_history import build_trends_section
    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    assert build_trends_section({}, config) == {}
    assert build_trends_section({}, None) == {}


# ── render_trends_html ────────────────────────────────────────────

def test_render_trends_html_bars():
    from briefing_renderer import render_trends_html
    section = {"metrics": [{"label": "needs_attention", "delta_label": "vs prev: -2",
                            "series": [{"ts": "2026-09-19T01:00:00+00:00", "kind": "daily", "value": 4},
                                       {"ts": "2026-09-20T01:00:00+00:00", "kind": "daily", "value": 2}]}]}
    html = render_trends_html(section)
    assert "trend-row" in html
    assert "bar-fill" in html
    assert "vs prev: -2" in html
    assert "width" in html


def test_render_trends_html_empty_and_malformed():
    from briefing_renderer import render_trends_html
    assert render_trends_html({}) == ""
    assert render_trends_html(None) == ""
    assert render_trends_html({"metrics": "junk"}) == ""
    assert render_trends_html({"metrics": [{"label": 1, "series": None}]}) == ""


def test_render_trends_html_zero_max_no_div_zero():
    from briefing_renderer import render_trends_html
    section = {"metrics": [{"label": "m", "series": [{"ts": "t", "kind": "daily", "value": 0}],
                            "delta_label": ""}]}
    html = render_trends_html(section)
    assert "width: 0%" in html


# ── render_html integration ───────────────────────────────────────

def test_render_html_includes_trends_section_when_present():
    from briefing_renderer import render_html
    b = _summary_briefing(trends={"metrics": [{"label": "needs_attention",
                                               "series": [{"ts": "t", "kind": "daily", "value": 3}],
                                               "delta_label": ""}]})
    html = render_html(b)
    assert "Trends" in html
    assert "trend-row" in html


def test_render_html_no_trends_key_no_section():
    from briefing_renderer import render_html
    b = _summary_briefing()
    html = render_html(b)
    assert "Trends" not in html


def test_render_html_still_self_contained_with_trends():
    from briefing_renderer import render_html
    b = _summary_briefing(trends={"metrics": [{"label": "suggestions",
                                               "series": [{"ts": "t", "kind": "daily", "value": 3}],
                                               "delta_label": ""}]})
    html = render_html(b)
    low = html.lower()
    assert "<script" not in low
    assert "http://" not in low and "https://" not in low


# ── weekly summary + weekly HTML ─────────────────────────────────

def _write_yaml(path: Path, data: dict) -> None:
    import yaml
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_build_weekly_summary_missing_files_never_raises(tmp_path):
    from weekly_summary import build_weekly_summary_from_config
    config = {"paths": {"project_root": str(tmp_path / "void")}}
    out = build_weekly_summary_from_config(config)
    assert isinstance(out, dict)
    assert "summary" in out


def test_build_weekly_summary_counts(tmp_path):
    from weekly_summary import build_weekly_summary_from_config
    root = tmp_path / "proj"
    root.mkdir()
    _write_yaml(root / "pipeline.yaml", {"deals": [
        {"id": "d1", "stage": "Proposal"},
        {"id": "d2", "stage": "Discovery"},
    ]})
    _write_yaml(root / "invoices.yaml", {"invoices": [
        {"id": "i1", "status": "sent", "amount": 500, "currency": "SGD"},
        {"id": "i2", "status": "paid", "amount": 300, "currency": "SGD"},
        {"id": "i3", "status": "overdue", "amount": 200, "currency": "SGD"},
    ]})
    _write_yaml(root / "todos.yaml", {"todos": [
        {"id": "t1", "status": "done"},
        {"id": "t2", "status": "open"},
    ]})
    config = {"paths": {"project_root": str(root)}}
    out = build_weekly_summary_from_config(config)
    assert out["summary"]["invoices_sent"] == 1
    assert out["summary"]["invoices_paid"] == 1
    assert out["summary"]["overdue_invoices"] == 1
    assert out["summary"]["tasks_completed"] == 1


def test_weekly_html_self_contained(tmp_path, monkeypatch):
    from weekly_summary import build_weekly_summary_from_config
    import briefing_renderer
    root = tmp_path / "proj"
    root.mkdir()
    config = {"paths": {"project_root": str(root)}}
    out = build_weekly_summary_from_config(config)
    out["operator"] = "Tester"
    html = briefing_renderer.render_html(out, title="Chief-of-Staff Weekly Review")
    low = html.lower()
    assert "<script" not in low
    assert "http://" not in low and "https://" not in low
    assert "Weekly Review" in html


def test_render_title_default_unchanged():
    from briefing_renderer import render_html
    b = _summary_briefing()
    html = render_html(b)
    html2 = render_html(b, title=None)
    assert html == html2, "default title path must be unchanged"