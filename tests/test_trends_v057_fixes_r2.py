#!/usr/bin/env python3
"""Round-2 fixes for trends + weekly HTML. Does not modify the attested contract file."""

from __future__ import annotations

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


# ── B1 deals_moved ──────────────────────────────────────────────


def test_deals_moved_reads_stage_history(tmp_path):
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    root.mkdir()
    today = _today()
    last_week = today - timedelta(days=8)
    _write_yaml(root / "pipeline.yaml", {"deals": [
        {
            "id": "d1",
            "stage": "Proposal",
            "stage_history": [
                {"stage": "Lead", "at": _iso(last_week)},
                {"stage": "Proposal", "at": _iso(today)},
            ],
        },
        {
            "id": "d2",
            "stage": "Lead",
            "stage_history": [{"stage": "Lead", "at": _iso(last_week)}],
        },
    ]})
    out = build_weekly_summary_from_config(cfg(root))
    assert out["pipeline"]["deals_moved"] == 1
    assert out["summary"]["deals_moved"] == 1


def test_deals_moved_updated_at_only_without_stage_history(tmp_path):
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    root.mkdir()
    _write_yaml(root / "pipeline.yaml", {"deals": [
        {"id": "d1", "stage": "Lead", "updated_at": _iso(_today())},
        {
            "id": "d2",
            "stage": "Proposal",
            "stage_history": [{"stage": "Proposal", "at": _iso(_today() - timedelta(days=20))}],
            "updated_at": _iso(_today()),
        },
    ]})
    out = build_weekly_summary_from_config(cfg(root))
    assert out["pipeline"]["deals_moved"] == 1


# ── B2 bookkeeping ──────────────────────────────────────────────


def test_bookkeeping_week_scope_direction_and_ar_ap(tmp_path):
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    root.mkdir()
    today = _today()
    last_week = today - timedelta(days=8)
    _write_yaml(root / "invoices.yaml", {"invoices": [
        {"id": "s1", "direction": "sent", "status": "sent", "amount": 100,
         "currency": "SGD", "issue_date": _iso(today)},
        {"id": "s-old", "direction": "sent", "status": "sent", "amount": 200,
         "currency": "SGD", "issue_date": _iso(last_week)},
        {"id": "r1", "direction": "received", "status": "received", "amount": 50,
         "currency": "SGD", "issue_date": _iso(today)},
        {"id": "p1", "direction": "sent", "status": "paid", "amount": 80,
         "currency": "SGD", "issue_date": _iso(last_week), "paid_date": _iso(today)},
        {"id": "p-old", "direction": "sent", "status": "paid", "amount": 90,
         "currency": "SGD", "issue_date": _iso(last_week), "paid_date": _iso(last_week)},
        {"id": "draft", "direction": "sent", "status": "draft", "amount": 999,
         "currency": "SGD", "issue_date": _iso(today)},
        {"id": "od", "direction": "sent", "status": "overdue", "amount": 40,
         "currency": "SGD", "issue_date": _iso(last_week)},
        {"id": "ap", "direction": "received", "status": "approved", "amount": 25,
         "currency": "USD", "issue_date": _iso(last_week)},
    ]})
    out = build_weekly_summary_from_config(cfg(root))
    bk = out["bookkeeping"]
    assert bk["invoices_sent"] == 1
    assert bk["invoices_received"] == 1
    assert bk["invoices_paid"] == 1
    assert bk["overdue_invoices"] == 1
    assert "outstanding_totals" not in bk
    assert bk["outstanding_ar"] == {"SGD": 340}
    assert bk["outstanding_ap"] == {"SGD": 50, "USD": 25}


def test_bookkeeping_currency_from_config(tmp_path):
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    root.mkdir()
    _write_yaml(root / "invoices.yaml", {"invoices": [
        {"id": "s1", "direction": "sent", "status": "sent", "amount": 10,
         "issue_date": _iso(_today())},
    ]})
    out = build_weekly_summary_from_config(cfg(root, bookkeeping={"base_currency": "USD"}))
    assert out["bookkeeping"]["outstanding_ar"] == {"USD": 10}


# ── B3 weekly renderer ──────────────────────────────────────────


def _weekly_payload() -> dict:
    return {
        "kind": "weekly",
        "generated_at": "2026-09-20T01:00:00+00:00",
        "week": {"start": "2026-09-14", "end": "2026-09-20"},
        "operator": "Tester",
        "summary": {
            "deals_moved": 1,
            "invoices_sent": 3,
            "invoices_paid": 1,
            "overdue_invoices": 0,
            "tasks_completed": 2,
            "tasks_carry_over": 1,
            "wiki_pages_changed": 1,
        },
        "pipeline": {
            "deals_by_stage": {"Proposal": 2, "Discovery": 1},
            "total_deals": 3,
            "deals_moved": 1,
        },
        "bookkeeping": {
            "invoices_sent": 3,
            "invoices_received": 1,
            "invoices_paid": 1,
            "overdue_invoices": 0,
            "outstanding_ar": {"SGD": 1200},
            "outstanding_ap": {"SGD": 75},
        },
        "tasks": {"tasks_completed": 2, "tasks_carry_over": 1, "tasks_overdue_open": 0},
        "knowledge": {"wiki_pages_created": 0, "wiki_pages_updated": 1, "wiki_pages_changed": 1},
        "expenses": {"expenses": [{"id": "e1", "vendor": "Delta", "amount": 12, "currency": "SGD"}]},
    }


def test_weekly_html_contains_stage_and_invoice_count():
    from briefing_renderer import render_html, render_text

    html = render_html(_weekly_payload(), title="Chief-of-Staff Weekly Review")
    assert "Proposal" in html
    assert "3" in html
    assert "Outstanding AR" in html
    assert "1200" in html
    assert "All clear" not in html
    assert "No overdue invoices." not in html
    assert "No stale deals." not in html
    assert "Good morning" not in html
    low = html.lower()
    assert "<script" not in low
    assert "http://" not in low and "https://" not in low
    text = render_text(_weekly_payload())
    assert "Good morning" not in text
    assert "Proposal" in text
    assert "Moved this week: 1" in text


def test_daily_html_without_trends_omits_trend_css():
    from briefing_renderer import render_html

    html = render_html({
        "operator": "Tester",
        "summary": {"needs_attention": 0, "pending_approvals": 0, "suggestions": 0,
                    "classified_emails": 0, "system_warnings": 0},
        "sections": {},
    })
    assert ".trend-row" not in html
    assert "trend-row" not in html


# ── M1 mutate_kv ────────────────────────────────────────────────


def test_capture_uses_mutate_kv_and_keeps_both_kinds(tmp_path, monkeypatch):
    import state_db
    from trend_history import capture_snapshot, TREND_KV_STORE, TREND_ROOT_KEY

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    calls = []
    orig = state_db.mutate_kv

    def spy(store_name, mutate_fn, **kwargs):
        # NOTE (operator re-attestation, 2026-09-20): the spy must NOT delegate to
        # the module-level mutate_kv — that function writes a YAML mirror + backups,
        # which capture_snapshot must never do (R3-1/R4-1: method-only writes).
        calls.append(store_name)
        with state_db.StateDB(config) as db:
            return db.mutate_kv(store_name, mutate_fn)

    monkeypatch.setattr(state_db, "mutate_kv", spy)
    briefing = {
        "summary": {"needs_attention": 2, "pending_approvals": 0, "suggestions": 0,
                    "classified_emails": 0, "system_warnings": 0},
        "sections": {},
    }
    assert capture_snapshot(briefing, config, kind="daily") is not None
    assert capture_snapshot(briefing, config, kind="weekly") is not None
    # Re-attested (2026-09-20): capture_snapshot must NOT call the module-level fn.
    assert calls == []
    assert not (root / "briefing_trends.yaml").exists()
    stored = state_db.StateDB(config).get_kv(TREND_KV_STORE)
    kinds = {s["kind"] for s in stored[TREND_ROOT_KEY]}
    assert kinds == {"daily", "weekly"}


# ── M2 wiki frontmatter ─────────────────────────────────────────


def test_wiki_created_outside_week_updated_inside(tmp_path):
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    today = _today()
    (wiki / "page.md").write_text(
        f"---\ncreated: 2020-01-01\nupdated: {today.isoformat()}\n---\nbody\n",
        encoding="utf-8",
    )
    out = build_weekly_summary_from_config(cfg(root, paths={"project_root": str(root), "wiki_path": str(wiki)}))
    assert out["knowledge"]["wiki_pages_created"] == 0
    assert out["knowledge"]["wiki_pages_updated"] == 1
    assert out["knowledge"]["wiki_pages_changed"] == 1


def test_wiki_without_frontmatter_dates_counted_once_by_mtime(tmp_path):
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "note.md").write_text("# no dates\n", encoding="utf-8")
    out = build_weekly_summary_from_config(cfg(root, paths={"project_root": str(root), "wiki_path": str(wiki)}))
    assert out["knowledge"]["wiki_pages_changed"] == 1
    assert out["knowledge"]["wiki_pages_created"] + out["knowledge"]["wiki_pages_updated"] == 1


# ── M3 YAML-first + divergence + malformed fallback ─────────────


def test_sources_divergence_yaml_two_store_zero(tmp_path):
    from state_db import StateDB
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    StateDB(config).put_kv("pipeline", {"deals": []})
    _write_yaml(root / "pipeline.yaml", {"deals": [
        {"id": "d1", "stage": "Proposal"},
        {"id": "d2", "stage": "Discovery"},
    ]})
    out = build_weekly_summary_from_config(config)
    assert out["sources"]["pipeline"] == {
        "yaml_records": 2,
        "store_records": 0,
        "divergence": True,
    }


def test_malformed_yaml_falls_through_to_store(tmp_path):
    from state_db import StateDB
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    StateDB(config).set_kv("pipeline", {"deals": [{"id": "from-store", "stage": "Lead"}]})
    (root / "pipeline.yaml").write_text("{this is: [not: yaml", encoding="utf-8")
    out = build_weekly_summary_from_config(config)
    assert out["pipeline"]["total_deals"] == 1
    assert out["pipeline"]["deals_by_stage"]["Lead"] == 1


# ── M4 flattening ───────────────────────────────────────────────


def _realistic_daily() -> dict:
    return {
        "summary": {
            "needs_attention": 1,
            "pending_approvals": 0,
            "suggestions": 0,
            "classified_emails": 0,
            "system_warnings": 0,
        },
        "sections": {
            "pipeline": {
                "active_deals": 4,
                "stale_deals": 1,
                "oldest_stale_id": "d1",
                "oldest_stale_days": 20,
                "oldest_stale_stage": "Proposal",
                "recently_moved": 1,
                "pending_crm_actions": 0,
                "contract_signed_no_invoice": 0,
                "invoiced_not_paid": 0,
            },
            "bookkeeper": {
                "candidates_found": 2,
                "candidates_needs_review": 1,
                "duplicate_warnings": 0,
                "pending_record_actions": 0,
                "outstanding_ap": "150.50",
                "outstanding_ar": "1200",
                "overdue_count": 1,
            },
        },
    }


def test_flatten_realistic_payload_prefixed_and_coerced(tmp_path):
    from trend_history import build_trends_section, capture_snapshot

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    snap = capture_snapshot(_realistic_daily(), config)
    assert snap is not None
    counters = snap["counters"]
    assert counters["pipeline.active_deals"] == 4
    assert counters["bookkeeper.outstanding_ar"] == 1200
    assert counters["bookkeeper.outstanding_ap"] == 150.5
    assert "outstanding_ar" not in counters
    section = build_trends_section(_realistic_daily(), config)
    labels = {m["label"] for m in section["metrics"]}
    assert "pipeline.active_deals" in labels
    assert "bookkeeper.outstanding_ar" in labels
    ar = next(m for m in section["metrics"] if m["label"] == "bookkeeper.outstanding_ar")
    assert ar["series"][-1]["value"] == 1200


# ── MINOR / NIT ─────────────────────────────────────────────────


def test_series_sorts_by_parsed_timestamp(tmp_path):
    from state_db import StateDB
    from trend_history import TREND_KV_STORE, TREND_ROOT_KEY, get_series

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    StateDB(config).set_kv(TREND_KV_STORE, {TREND_ROOT_KEY: [
        {"ts": "2026-09-19T01:00:00+02:00", "kind": "daily", "counters": {"needs_attention": 1}},
        {"ts": "2026-09-19T00:30:00+00:00", "kind": "daily", "counters": {"needs_attention": 2}},
    ]})
    series = get_series(config, "needs_attention", days=30)
    assert [p["value"] for p in series] == [1, 2]


def test_prune_and_same_day_replace_drop_unparseable(tmp_path):
    from state_db import StateDB
    from trend_history import TREND_KV_STORE, TREND_ROOT_KEY, capture_snapshot

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    StateDB(config).set_kv(TREND_KV_STORE, {TREND_ROOT_KEY: [
        {"ts": "not-a-date", "kind": "daily", "counters": {"needs_attention": 9}},
    ]})
    briefing = {
        "summary": {"needs_attention": 1, "pending_approvals": 0, "suggestions": 0,
                    "classified_emails": 0, "system_warnings": 0},
        "sections": {},
    }
    capture_snapshot(briefing, config)
    snaps = StateDB(config).get_kv(TREND_KV_STORE)[TREND_ROOT_KEY]
    assert all(_parseable(s.get("ts")) for s in snaps)
    assert all(s["counters"]["needs_attention"] == 1 for s in snaps if s.get("kind") == "daily")


def _parseable(ts) -> bool:
    from trend_history import _parse_ts
    return _parse_ts(ts) is not None


def test_float_delta_rounded():
    from trend_history import _delta_label

    label = _delta_label([{"value": 0.1}, {"value": 0.3}])
    assert label == "vs prev: +0.2"
    assert "999" not in label


def test_load_config_quiet_silences_missing(tmp_path, capsys):
    from config_loader import load_config

    result = load_config(str(tmp_path / "missing.yaml"), quiet=True)
    assert result is None
    assert capsys.readouterr().err == ""


def test_attach_trends_dry_run_skips_capture(tmp_path, monkeypatch):
    from daily_briefing import _attach_trends

    captured = []

    def fake_capture(*args, **kwargs):
        captured.append(True)
        return None

    monkeypatch.setattr("trend_history.capture_snapshot", fake_capture)
    monkeypatch.setattr("trend_history.build_trends_section", lambda *a, **k: {})
    briefing = {"summary": {"needs_attention": 0}}
    _attach_trends(briefing, cfg(tmp_path), capture=False)
    assert captured == []
    _attach_trends(briefing, cfg(tmp_path), capture=True)
    assert captured == [True]


def test_wrapper_registers_sys_modules():
    import importlib.util
    import sys as _sys

    _sys.modules.pop("cos_weekly_summary", None)
    path = PLUGIN_ROOT / "skills" / "weekly-review" / "scripts" / "weekly_summary.py"
    spec = importlib.util.spec_from_file_location("weekly_summary_wrapper_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "cos_weekly_summary" in _sys.modules


def test_snapshot_health_warns_when_empty(tmp_path):
    from trend_history import snapshot_health

    root = tmp_path / "proj"
    root.mkdir()
    health = snapshot_health(cfg(root))
    assert health["status"] == "warn"


def test_render_trends_html_survives_import_error(monkeypatch):
    import builtins
    from briefing_renderer import render_trends_html

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "trend_history":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert render_trends_html({"metrics": []}) == ""


def test_kind_weekly_is_set(tmp_path):
    from weekly_summary import build_weekly_summary_from_config

    out = build_weekly_summary_from_config(cfg(tmp_path / "void"))
    assert out["kind"] == "weekly"
    assert "week" in out
