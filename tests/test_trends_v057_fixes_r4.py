#!/usr/bin/env python3
"""Round-4 fixes for trends + weekly HTML. Does not modify attested contract files."""

from __future__ import annotations

import inspect
import sys
from datetime import date, timedelta
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


def _today() -> date:
    return date.today()


def _iso(day: date) -> str:
    return day.isoformat()


def _weekly_skeleton(**overrides) -> dict:
    b = {
        "kind": "weekly",
        "generated_at": "2026-09-20T01:00:00+00:00",
        "week": {"start": "2026-09-14", "end": "2026-09-20"},
        "operator": "Tester",
        "summary": {
            "deals_moved": 0, "invoices_sent": 0, "invoices_paid": 0,
            "overdue_invoices": 0, "tasks_completed": 0,
            "tasks_carry_over": 0, "wiki_pages_changed": 0,
        },
        "pipeline": {},
        "bookkeeping": {},
        "tasks": {},
        "knowledge": {},
        "expenses": {},
    }
    b.update(overrides)
    return b


# ── R4-1 no monkeypatch shim ────────────────────────────────────


def test_capture_snapshot_source_has_no_test_detection():
    import trend_history

    src = inspect.getsource(trend_history.capture_snapshot)
    assert "patched" not in src
    assert "__module__" not in src
    assert "getattr(_state_db" not in src
    full = Path(trend_history.__file__).read_text(encoding="utf-8")
    assert "getattr(patched" not in full
    assert '__module__, "state_db"' not in full


def test_capture_snapshot_ignores_module_level_mutate_kv(tmp_path, monkeypatch):
    import state_db
    from trend_history import capture_snapshot

    root = tmp_path / "proj"
    root.mkdir()
    calls = []

    def spy(store_name, mutate_fn, **kwargs):
        calls.append(store_name)
        raise AssertionError("module-level mutate_kv must not be called")

    monkeypatch.setattr(state_db, "mutate_kv", spy)
    snap = capture_snapshot({
        "summary": {"needs_attention": 1, "pending_approvals": 0, "suggestions": 0,
                    "classified_emails": 0, "system_warnings": 0},
        "sections": {},
    }, cfg(root))
    assert snap is not None
    assert calls == []
    assert not (root / "briefing_trends.yaml").exists()


# ── R4-2 / R4-7 / R4-9 per-currency AR/AP, drafts, shared parser ─


def test_collect_bookkeeper_stats_per_currency_includes_drafts(tmp_path):
    from briefing_sources import collect_bookkeeper_stats

    root = tmp_path / "proj"
    root.mkdir()
    today = _today()
    _write_yaml(root / "invoices.yaml", {"invoices": [
        {"id": "usd", "direction": "sent", "status": "sent", "amount": 100,
         "currency": "USD", "due_date": _iso(today + timedelta(days=5))},
        {"id": "jpy", "direction": "sent", "status": "sent", "amount": 100,
         "currency": "JPY", "due_date": _iso(today + timedelta(days=5))},
        {"id": "draft", "direction": "sent", "status": "draft", "amount": 8,
         "currency": "USD", "due_date": _iso(today - timedelta(days=2))},
        {"id": "voided", "direction": "sent", "status": "void", "amount": 50,
         "currency": "USD"},
        {"id": "written", "direction": "sent", "status": "written_off", "amount": 7,
         "currency": "JPY"},
        {"id": "annot", "direction": "received", "status": "received", "amount": 25,
         "currency": "SGD", "due_date": "2020-01-01 (est)"},
    ]})
    stats = collect_bookkeeper_stats(cfg(root))
    assert stats["outstanding_ar"] == {"USD": 108, "JPY": 100}
    assert stats["outstanding_ap"] == {"SGD": 25}
    assert stats["overdue_count"] == 2


def test_extract_counters_outstanding_uses_currency_keys(tmp_path):
    from trend_history import _extract_counters, build_trends_section, capture_snapshot

    briefing = {
        "summary": {"needs_attention": 0, "pending_approvals": 0, "suggestions": 0,
                    "classified_emails": 0, "system_warnings": 0},
        "sections": {
            "bookkeeper": {
                "outstanding_ar": {"USD": 100, "JPY": 100},
                "outstanding_ap": {"SGD": 25},
                "overdue_count": 1,
            }
        },
    }
    counters = _extract_counters(briefing)
    assert counters["bookkeeper.outstanding_ar::USD"] == 100
    assert counters["bookkeeper.outstanding_ar::JPY"] == 100
    assert counters["bookkeeper.outstanding_ap::SGD"] == 25
    assert "bookkeeper.outstanding_ar" not in counters
    assert "bookkeeper.outstanding_ap" not in counters

    root = tmp_path / "proj"
    root.mkdir()
    assert capture_snapshot(briefing, cfg(root)) is not None
    section = build_trends_section(briefing, cfg(root))
    labels = [m["label"] for m in section["metrics"]]
    assert "bookkeeper.outstanding_ar::JPY" in labels
    assert "bookkeeper.outstanding_ar::USD" in labels
    assert "bookkeeper.outstanding_ap::SGD" in labels


def test_format_outstanding_bucket_single_and_multi():
    from cos_helpers import _format_outstanding_bucket

    assert _format_outstanding_bucket({"SGD": 1200}) == "SGD 1200"
    assert _format_outstanding_bucket({"USD": 100, "JPY": 100}) == "JPY 100 / USD 100"
    assert _format_outstanding_bucket({}) == "0"
    assert _format_outstanding_bucket(None) == "0"


# ── R4-3 status aliases ─────────────────────────────────────────


def test_todo_status_aliases_completed_and_pending(tmp_path):
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    root.mkdir()
    today = _today()
    _write_yaml(root / "todos.yaml", {"todos": [
        {"id": "t-done", "status": "completed", "completed_at": _iso(today)},
        {"id": "t-open", "status": "pending", "due": _iso(today - timedelta(days=1))},
        {"id": "t-unknown", "status": "blocked"},
    ]})
    out = build_weekly_summary_from_config(cfg(root))
    assert out["tasks"]["tasks_completed"] == 1
    assert out["tasks"]["tasks_done_total"] == 1
    assert out["tasks"]["tasks_carry_over"] == 1
    assert out["tasks"]["tasks_overdue_open"] == 1


# ── R4-4 currency-list path through _put ────────────────────────


def test_flatten_currency_list_collision_is_order_independent():
    from trend_history import _flatten_mapping

    counters: dict = {}
    _flatten_mapping(
        {"aging": [{"currency": "SGD", "amount": 1}], "aging::SGD": 9},
        counters,
        "bookkeeper",
    )
    assert "bookkeeper.aging::SGD" not in counters

    counters_rev: dict = {}
    _flatten_mapping(
        {"aging::SGD": 9, "aging": [{"currency": "SGD", "amount": 1}]},
        counters_rev,
        "bookkeeper",
    )
    assert "bookkeeper.aging::SGD" not in counters_rev


def test_flatten_currency_list_writes_when_unambiguous():
    from trend_history import _flatten_mapping

    counters: dict = {}
    _flatten_mapping(
        {"aging": [{"currency": "SGD", "amount": 9}]},
        counters,
        "bookkeeper",
    )
    assert counters["bookkeeper.aging::SGD"] == 9


# ── R4-5 isolated section breadcrumb ────────────────────────────


def test_section_failure_records_envelope_exception(tmp_path, monkeypatch):
    import weekly_summary as w

    root = tmp_path / "proj"
    root.mkdir()
    _write_yaml(root / "pipeline.yaml", {"deals": [{"id": "d1", "stage": "Lead"}]})

    def boom(*_args, **_kwargs):
        raise RuntimeError("knowledge failed")

    monkeypatch.setattr(w, "_knowledge_section", boom)
    out = w.build_weekly_summary_from_config(cfg(root))
    assert out["pipeline"]["total_deals"] == 1
    assert out["knowledge"] == {}
    assert out["exceptions"]
    assert out["exceptions"][0]["section"] == "knowledge"
    assert out["exceptions"][0]["error"] == "RuntimeError"


def test_isolated_section_calls_note_exception(monkeypatch):
    import weekly_summary as w
    import trend_history

    seen = []

    def spy(where, exc):
        seen.append((where, type(exc).__name__))

    monkeypatch.setattr(trend_history, "note_exception", spy)
    exceptions: list = []

    def boom():
        raise ValueError("nope")

    assert w._isolated_section(boom, name="tasks", exceptions=exceptions) == {}
    assert seen == [("weekly_summary.tasks", "ValueError")]
    assert exceptions[0]["section"] == "tasks"


# ── R4-6 YAML fallthrough disclosure ────────────────────────────


def test_unreadable_yaml_records_store_fallback(tmp_path):
    from briefing_renderer import render_weekly_html
    from state_db import StateDB
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    StateDB(config).set_kv("pipeline", {"deals": [{"id": "from-store", "stage": "Lead"}]})
    _write_yaml(root / "pipeline.yaml", {"deals": None})
    out = build_weekly_summary_from_config(config)
    src = out["sources"]["pipeline"]
    assert src["fallback"] == "store"
    assert src.get("reason")
    html = render_weekly_html(out)
    assert "unreadable" in html.lower()
    assert "pipeline" in html
    assert "using store" in html.lower()


# ── R4-8 parse_date space-branch fallback ───────────────────────


def test_parse_date_space_annotation_falls_back_to_prefix():
    from weekly_summary import _parse_date

    assert _parse_date("2026-09-15 (est)") == date(2026, 9, 15)
    assert _parse_date("2026-09-15 20:00:00") == date(2026, 9, 15)


# ── R4-10 divergence chip blanks ────────────────────────────────


def test_divergence_chip_renders_missing_counts_as_zero():
    from briefing_renderer import render_weekly_html

    html = render_weekly_html(_weekly_skeleton(sources={
        "pipeline": {"divergence": True},
    }))
    assert "YAML 0" in html
    assert "store 0" in html
    assert "pipeline" in html
