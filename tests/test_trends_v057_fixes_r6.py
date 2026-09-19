#!/usr/bin/env python3
"""Round-6 fixes for trends settlement guards + daily disclosure.

Does not modify attested contract files.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
DAILY = PLUGIN_ROOT / "skills" / "daily-briefing" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(DAILY) not in sys.path:
    sys.path.insert(0, str(DAILY))


def cfg(root: Path, **extra) -> dict:
    data = {"paths": {"project_root": str(root)}}
    data.update(extra)
    return data


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _summary(**bookkeeper) -> dict:
    return {
        "summary": {
            "needs_attention": 0,
            "pending_approvals": 0,
            "suggestions": 0,
            "classified_emails": 0,
            "system_warnings": 0,
        },
        "sections": {"bookkeeper": bookkeeper},
    }


def _daily_briefing(**bookkeeper) -> dict:
    return {
        "generated_at": "2026-09-20T01:00:00+00:00",
        "operator": "Tester",
        "summary": {
            "needs_attention": 0,
            "pending_approvals": 0,
            "suggestions": 0,
            "classified_emails": 0,
            "system_warnings": 0,
        },
        "sections": {"bookkeeper": bookkeeper},
    }


# ── R6-1 settlement guards ──────────────────────────────────────


def test_degraded_snapshot_without_bookkeeper_is_not_settlement():
    from trend_history import _points_from_snaps

    now = _now()
    metric = "bookkeeper.outstanding_ar::USD"
    snaps = [
        {
            "ts": (now - timedelta(days=1)).isoformat(),
            "kind": "daily",
            "counters": {metric: 100},
        },
        {
            "ts": now.isoformat(),
            "kind": "daily",
            "counters": {"needs_attention": 0},
        },
    ]
    series = _points_from_snaps(
        snaps, metric, now - timedelta(days=7), kind="daily",
    )
    assert [p["value"] for p in series] == [100]
    assert not any(p.get("carried") for p in series)


def test_dropped_metric_is_not_settlement():
    from trend_history import _points_from_snaps

    now = _now()
    metric = "bookkeeper.outstanding_ar::USD"
    snaps = [
        {
            "ts": (now - timedelta(days=1)).isoformat(),
            "kind": "daily",
            "counters": {metric: 100, "bookkeeper.overdue_count": 1},
        },
        {
            "ts": now.isoformat(),
            "kind": "daily",
            "counters": {"bookkeeper.overdue_count": 1},
            "dropped": [metric],
        },
    ]
    series = _points_from_snaps(
        snaps, metric, now - timedelta(days=7), kind="daily",
    )
    assert [p["value"] for p in series] == [100]
    assert not any(p.get("carried") for p in series)


def test_bookkeeper_block_without_child_still_settles():
    from trend_history import _extract_counters, _points_from_snaps

    now = _now()
    first = _extract_counters(_summary(outstanding_ar={"USD": 100}, overdue_count=1))
    second = _extract_counters(_summary(outstanding_ar={}, overdue_count=0))
    snaps = [
        {"ts": (now - timedelta(days=1)).isoformat(), "kind": "daily", "counters": first},
        {"ts": now.isoformat(), "kind": "daily", "counters": second},
    ]
    series = _points_from_snaps(
        snaps, "bookkeeper.outstanding_ar::USD", now - timedelta(days=7), kind="daily",
    )
    assert [p["value"] for p in series] == [100, 0]
    assert series[-1].get("carried") is True


# ── R6-2 dropped_count is distinct daily dest keys ──────────────


def test_dropped_count_dedupes_and_ignores_weekly(tmp_path):
    from state_db import StateDB
    from trend_history import TREND_KV_STORE, TREND_ROOT_KEY, build_trends_section

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    now = _now()
    snaps = []
    for i in range(5):
        ts = (now - timedelta(days=i)).isoformat()
        snaps.append({
            "ts": ts,
            "kind": "daily",
            "counters": {"needs_attention": i, "bookkeeper.overdue_count": 0},
            "dropped": ["pipeline.aging::SGD"],
        })
        snaps.append({
            "ts": ts,
            "kind": "weekly",
            "counters": {"needs_attention": i},
            "dropped": ["pipeline.aging::SGD"],
        })
    StateDB(config).set_kv(TREND_KV_STORE, {TREND_ROOT_KEY: snaps})
    section = build_trends_section({}, config)
    assert section["dropped_count"] == 1


def test_render_trends_html_emits_dropped_count_chip():
    from briefing_renderer import render_trends_html

    html = render_trends_html({
        "metrics": [{
            "label": "needs_attention",
            "series": [{"ts": "t", "kind": "daily", "value": 2}],
            "delta_label": "",
        }],
        "dropped_count": 1,
    })
    assert "dropped key" in html
    assert "1 dropped" in html


# ── R6-3 daily sources disclosure on bookkeeper.sources ─────────


def test_daily_render_reads_bookkeeper_sources_not_top_level():
    from briefing_renderer import render_html, render_markdown, render_text

    briefing = _daily_briefing(
        overdue_count=0,
        sources={"invoices": {"fallback": "store", "reason": "not a mapping"}},
    )
    html = render_html(briefing)
    text = render_text(briefing)
    md = render_markdown(briefing)
    assert "Data divergence" in html
    assert "invoices" in html
    assert "using store" in html.lower()
    assert "Data divergence" in text
    assert "invoices" in text
    assert "Data divergence" in md
    assert "briefing.get('sources')" not in html
    assert briefing.get("sources") is None


# ── R6-4 pass resolved root into _load_records ──────────────────


def test_collect_bookkeeper_stats_uses_env_root_when_config_has_no_paths(
    tmp_path, monkeypatch,
):
    from briefing_sources import collect_bookkeeper_stats

    root = tmp_path / "proj"
    root.mkdir()
    _write_yaml(root / "invoices.yaml", {"invoices": [
        {"id": "env", "direction": "sent", "status": "sent",
         "amount": 40, "currency": "EUR"},
    ]})
    monkeypatch.setenv("CHIEF_OF_STAFF_PROJECT_ROOT", str(root))
    stats = collect_bookkeeper_stats({})
    assert stats["outstanding_ar"] == {"EUR": 40}


def test_load_records_optional_root_leaves_weekly_config_only(tmp_path, monkeypatch):
    from weekly_summary import _load_records

    config_root = tmp_path / "config"
    env_root = tmp_path / "env"
    config_root.mkdir()
    env_root.mkdir()
    _write_yaml(config_root / "invoices.yaml", {"invoices": [
        {"id": "from-config", "direction": "sent", "status": "sent",
         "amount": 1, "currency": "USD"},
    ]})
    _write_yaml(env_root / "invoices.yaml", {"invoices": [
        {"id": "from-env", "direction": "sent", "status": "sent",
         "amount": 9, "currency": "USD"},
    ]})
    monkeypatch.setenv("CHIEF_OF_STAFF_PROJECT_ROOT", str(env_root))
    rows = _load_records(cfg(config_root), "invoices.yaml", "invoices", "invoices")
    assert rows[0]["id"] == "from-config"

    rows_with_root = _load_records(
        {}, "invoices.yaml", "invoices", "invoices", root=env_root,
    )
    assert rows_with_root[0]["id"] == "from-env"
