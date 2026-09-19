#!/usr/bin/env python3
"""Round-5 fixes for trends + daily AR/AP. Does not modify attested contract files."""

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


# ── R5-1 settled currency reaches 0 ─────────────────────────────


def test_settled_currency_series_is_100_then_0():
    from trend_history import _extract_counters, _points_from_snaps

    now = _now()
    first = _extract_counters(_summary(outstanding_ar={"USD": 100}))
    second = _extract_counters(_summary(outstanding_ar={}))
    snaps = [
        {"ts": (now - timedelta(days=1)).isoformat(), "kind": "daily", "counters": first},
        {"ts": now.isoformat(), "kind": "daily", "counters": second},
    ]
    series = _points_from_snaps(
        snaps, "bookkeeper.outstanding_ar::USD", now - timedelta(days=7), kind="daily",
    )
    assert [p["value"] for p in series] == [100, 0]
    assert series[-1].get("carried") is True


def test_settled_currency_via_get_series_and_build_trends(tmp_path):
    from state_db import StateDB
    from trend_history import (
        TREND_KV_STORE,
        TREND_ROOT_KEY,
        build_trends_section,
        capture_snapshot,
        get_series,
    )

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    now = _now()
    first = capture_snapshot(_summary(outstanding_ar={"USD": 100}, overdue_count=1), config)
    assert first is not None
    stored = StateDB(config).get_kv(TREND_KV_STORE)
    stored[TREND_ROOT_KEY][0]["ts"] = (now - timedelta(days=1)).isoformat()
    StateDB(config).set_kv(TREND_KV_STORE, stored)

    second = capture_snapshot(_summary(outstanding_ar={}, overdue_count=0), config)
    assert second is not None
    assert "bookkeeper.outstanding_ar::USD" not in second["counters"]

    series = get_series(config, "bookkeeper.outstanding_ar::USD", days=30, kind="daily")
    assert [p["value"] for p in series] == [100, 0]
    assert series[-1].get("carried") is True

    section = build_trends_section({}, config)
    usd = next(m for m in section["metrics"] if m["label"] == "bookkeeper.outstanding_ar::USD")
    assert [p["value"] for p in usd["series"]] == [100, 0]


def test_no_later_snapshot_is_not_synthetic_zero():
    from trend_history import _points_from_snaps

    now = _now()
    snaps = [
        {
            "ts": (now - timedelta(days=1)).isoformat(),
            "kind": "daily",
            "counters": {"bookkeeper.outstanding_ar::USD": 100},
        },
    ]
    series = _points_from_snaps(
        snaps, "bookkeeper.outstanding_ar::USD", now - timedelta(days=7), kind="daily",
    )
    assert [p["value"] for p in series] == [100]
    assert not any(p.get("carried") for p in series)


def test_scalar_overdue_count_is_not_carried_to_zero():
    from trend_history import _points_from_snaps

    now = _now()
    snaps = [
        {
            "ts": (now - timedelta(days=1)).isoformat(),
            "kind": "daily",
            "counters": {"bookkeeper.overdue_count": 2, "bookkeeper.candidates_found": 1},
        },
        {
            "ts": now.isoformat(),
            "kind": "daily",
            "counters": {"bookkeeper.candidates_found": 0},
        },
    ]
    series = _points_from_snaps(
        snaps, "bookkeeper.overdue_count", now - timedelta(days=7), kind="daily",
    )
    assert [p["value"] for p in series] == [2]


def test_synthetic_zero_does_not_rewrite_stored_counters(tmp_path):
    from state_db import StateDB
    from trend_history import TREND_KV_STORE, TREND_ROOT_KEY, get_series

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    now = _now()
    StateDB(config).set_kv(TREND_KV_STORE, {TREND_ROOT_KEY: [
        {
            "ts": (now - timedelta(days=1)).isoformat(),
            "kind": "daily",
            "counters": {"bookkeeper.outstanding_ar::USD": 100, "needs_attention": 0},
        },
        {
            "ts": now.isoformat(),
            "kind": "daily",
            "counters": {"needs_attention": 0},
        },
    ]})
    series = get_series(config, "bookkeeper.outstanding_ar::USD", days=30, kind="daily")
    assert [p["value"] for p in series] == [100, 0]
    stored = StateDB(config).get_kv(TREND_KV_STORE)[TREND_ROOT_KEY]
    assert "bookkeeper.outstanding_ar::USD" not in stored[-1]["counters"]


# ── R5-2 dropped-keys disclosure ────────────────────────────────


def test_extract_counters_attaches_dropped_not_as_key():
    from trend_history import _extract_counters

    counters = _extract_counters(_summary(
        aging=[{"currency": "SGD", "amount": 1}],
        **{"aging::SGD": 9},
    ))
    assert counters is not None
    assert "bookkeeper.aging::SGD" not in counters
    assert "_dropped" not in counters
    assert "bookkeeper.aging::SGD" in getattr(counters, "_dropped", [])


def test_capture_snapshot_stores_dropped_on_envelope(tmp_path):
    from trend_history import build_trends_section, capture_snapshot

    root = tmp_path / "proj"
    root.mkdir()
    snap = capture_snapshot(_summary(
        outstanding_ar={"USD": 100},
        aging=[{"currency": "SGD", "amount": 1}],
        **{"aging::SGD": 9},
    ), cfg(root))
    assert snap is not None
    assert "bookkeeper.aging::SGD" not in snap["counters"]
    assert "bookkeeper.aging::SGD" in snap["dropped"]
    assert "_dropped" not in snap["counters"]
    section = build_trends_section({}, cfg(root))
    assert section["dropped_count"] >= 1


def test_capture_snapshot_omits_dropped_when_empty(tmp_path):
    from trend_history import capture_snapshot

    root = tmp_path / "proj"
    root.mkdir()
    snap = capture_snapshot(_summary(outstanding_ar={"USD": 100}), cfg(root))
    assert snap is not None
    assert "dropped" not in snap


# ── R5-3 daily AR/AP via _load_records ──────────────────────────


def test_collect_bookkeeper_stats_malformed_yaml_falls_through_to_store(tmp_path):
    from briefing_sources import collect_bookkeeper_stats
    from state_db import StateDB

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    StateDB(config).set_kv("invoices", {"invoices": [
        {"id": "from-store", "direction": "sent", "status": "sent",
         "amount": 50, "currency": "USD"},
    ]})
    (root / "invoices.yaml").write_text("- not a mapping\n", encoding="utf-8")
    stats = collect_bookkeeper_stats(config)
    assert stats["outstanding_ar"] == {"USD": 50}
    src = (stats.get("sources") or {}).get("invoices") or {}
    assert src.get("fallback") == "store"
    assert src.get("reason")


def test_collect_bookkeeper_stats_store_only_without_yaml(tmp_path):
    from briefing_sources import collect_bookkeeper_stats
    from state_db import StateDB

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    StateDB(config).set_kv("invoices", {"invoices": [
        {"id": "store", "direction": "sent", "status": "sent",
         "amount": 75, "currency": "JPY"},
    ]})
    stats = collect_bookkeeper_stats(config)
    assert stats["outstanding_ar"] == {"JPY": 75}


def test_collect_bookkeeper_stats_notes_exception(monkeypatch, tmp_path):
    import trend_history
    import weekly_summary as w
    from briefing_sources import collect_bookkeeper_stats

    root = tmp_path / "proj"
    root.mkdir()
    seen: list[str] = []

    def spy(where, exc):
        seen.append(where)

    def boom(*_args, **_kwargs):
        raise RuntimeError("invoice boom")

    monkeypatch.setattr(trend_history, "note_exception", spy)
    monkeypatch.setattr(w, "_load_records", boom)
    collect_bookkeeper_stats(cfg(root))
    assert any(item == "collect_bookkeeper_stats.invoices" for item in seen)


# ── R5-4 _amt(-0.001) → 0 ───────────────────────────────────────


def test_format_outstanding_bucket_negative_zero():
    from cos_helpers import _format_outstanding_bucket

    assert _format_outstanding_bucket({"USD": -0.001}) == "USD 0"
    assert _format_outstanding_bucket({"USD": -0.00}) == "USD 0"
    assert _format_outstanding_bucket({}) == "0"


# ── R5-5 hard-coded _DONE_STATUSES ──────────────────────────────


def test_done_statuses_is_hard_done_set():
    from schemas import TODO_STATUSES
    from weekly_summary import _DONE_STATUSES, _OPEN_STATUSES, _STATUS_ALIASES

    assert _DONE_STATUSES == frozenset({"done"})
    assert _DONE_STATUSES <= TODO_STATUSES
    assert not (_DONE_STATUSES & _OPEN_STATUSES)
    assert _STATUS_ALIASES["completed"] == "done"
    assert _STATUS_ALIASES["pending"] == "open"
