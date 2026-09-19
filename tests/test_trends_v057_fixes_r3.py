#!/usr/bin/env python3
"""Round-3 fixes for trends + weekly HTML. Does not modify attested contract files."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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
        "sections": {},
    }
    b.update(overrides)
    return b


# ── R3-1 capture_snapshot uses StateDB.mutate_kv (no YAML mirror) ─


def test_capture_snapshot_uses_statedb_mutate_kv_no_yaml(tmp_path, monkeypatch):
    from state_db import StateDB
    from trend_history import TREND_KV_STORE, capture_snapshot

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    calls = []
    orig = StateDB.mutate_kv

    def spy(self, store_name, mutate_fn, **kwargs):
        calls.append(store_name)
        return orig(self, store_name, mutate_fn, **kwargs)

    monkeypatch.setattr(StateDB, "mutate_kv", spy)
    snap = capture_snapshot(_summary_briefing(), config)
    assert snap is not None
    assert calls and all(c == TREND_KV_STORE for c in calls)
    assert not (root / "briefing_trends.yaml").exists()
    assert not list(root.glob(".backups/briefing_trends*.yaml"))


# ── R3-2 strict paid_date ───────────────────────────────────────


def test_undated_paid_invoice_not_counted_this_week(tmp_path):
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    root.mkdir()
    _write_yaml(root / "invoices.yaml", {"invoices": [
        {
            "id": "old-paid",
            "direction": "sent",
            "status": "paid",
            "amount": 300,
            "currency": "SGD",
            "issue_date": "2020-01-01",
        },
        {
            "id": "paid-this-week",
            "direction": "sent",
            "status": "paid",
            "amount": 80,
            "currency": "SGD",
            "issue_date": "2020-01-01",
            "paid_date": _iso(_today()),
        },
    ]})
    out = build_weekly_summary_from_config(cfg(root))
    assert out["bookkeeping"]["invoices_paid"] == 1
    assert out["summary"]["invoices_paid"] == 1


# ── R3-3 wiki decode + section isolation ────────────────────────


def test_non_utf8_wiki_keeps_pipeline_section(tmp_path):
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "bad.md").write_bytes(b"\xff\xfe not utf-8")
    _write_yaml(root / "pipeline.yaml", {"deals": [
        {"id": "d1", "stage": "Proposal"},
    ]})
    out = build_weekly_summary_from_config(
        cfg(root, paths={"project_root": str(root), "wiki_path": str(wiki)})
    )
    assert out["pipeline"]["deals_by_stage"]["Proposal"] == 1
    assert out["pipeline"]["total_deals"] == 1


def test_section_failure_does_not_blank_envelope(tmp_path, monkeypatch):
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


# ── R3-4 weekly HTML divergence ─────────────────────────────────


def test_weekly_html_renders_divergence_chip():
    from briefing_renderer import render_weekly_html

    briefing = {
        "kind": "weekly",
        "generated_at": "2026-09-20T01:00:00+00:00",
        "week": {"start": "2026-09-14", "end": "2026-09-20"},
        "operator": "Tester",
        "summary": {"deals_moved": 0, "invoices_sent": 0, "invoices_paid": 0,
                    "overdue_invoices": 0, "tasks_completed": 0,
                    "tasks_carry_over": 0, "wiki_pages_changed": 0},
        "pipeline": {},
        "bookkeeping": {},
        "tasks": {},
        "knowledge": {},
        "expenses": {},
        "sources": {
            "pipeline": {"yaml_records": 2, "store_records": 0, "divergence": True},
        },
    }
    html = render_weekly_html(briefing)
    assert "diverg" in html.lower()
    assert "pipeline" in html
    assert "YAML 2" in html
    assert "store 0" in html
    assert "<script" not in html.lower()


# ── R3-5 coerce overflow ────────────────────────────────────────


def test_coerce_numeric_overflow_returns_none():
    from trend_history import _coerce_numeric

    assert _coerce_numeric("outstanding_ar", "1e309") is None


# ── R3-6 flatten duplicate dest ─────────────────────────────────


def test_flatten_mapping_drops_ambiguous_dest():
    from trend_history import _flatten_mapping

    counters: dict = {}
    _flatten_mapping(
        {"deals_by_stage": {"Proposal": 2}, "stage::Proposal": 9},
        counters,
        "pipeline",
    )
    assert "pipeline.stage::Proposal" not in counters
    counters_rev: dict = {}
    _flatten_mapping(
        {"stage::Proposal": 9, "deals_by_stage": {"Proposal": 2}},
        counters_rev,
        "pipeline",
    )
    assert "pipeline.stage::Proposal" not in counters_rev


# ── R3-7 unknown direction excluded from AR/AP ──────────────────


def test_unknown_direction_does_not_inflate_ar(tmp_path):
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    root.mkdir()
    _write_yaml(root / "invoices.yaml", {"invoices": [
        {"id": "x1", "status": "approved", "amount": 99, "currency": "SGD",
         "issue_date": _iso(_today())},
        {"id": "s1", "direction": "sent", "status": "sent", "amount": 10,
         "currency": "SGD", "issue_date": _iso(_today())},
    ]})
    bk = build_weekly_summary_from_config(cfg(root))["bookkeeping"]
    assert bk["outstanding_ar"] == {"SGD": 10}
    assert "SGD" not in (bk.get("outstanding_ap") or {})
    assert bk.get("outstanding_unknown") == {"SGD": 99}


# ── R3-8 wiki mtime fallback when updated missing ───────────────


def test_wiki_stale_created_mtime_counts_updated(tmp_path):
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "page.md").write_text("---\ncreated: 2020-01-01\n---\nbody\n", encoding="utf-8")
    out = build_weekly_summary_from_config(
        cfg(root, paths={"project_root": str(root), "wiki_path": str(wiki)})
    )
    assert out["knowledge"]["wiki_pages_created"] == 0
    assert out["knowledge"]["wiki_pages_updated"] == 1


# ── R3-9 naive datetime is local wall clock ─────────────────────


def test_parse_date_naive_datetime_keeps_calendar_day():
    from weekly_summary import _parse_date

    parsed = _parse_date(datetime(2026, 9, 15, 20, 0, 0))
    assert parsed == date(2026, 9, 15)


# ── R3-10 yaml structural errors fall through ───────────────────


def test_yaml_non_list_falls_through_to_store(tmp_path):
    from state_db import StateDB
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    StateDB(config).set_kv("pipeline", {"deals": [{"id": "from-store", "stage": "Lead"}]})
    _write_yaml(root / "pipeline.yaml", {"deals": None})
    out = build_weekly_summary_from_config(config)
    assert out["pipeline"]["total_deals"] == 1
    assert out["pipeline"]["deals_by_stage"]["Lead"] == 1


def test_yaml_records_absent_is_none(tmp_path):
    from weekly_summary import _yaml_records

    assert _yaml_records(tmp_path / "missing.yaml", "deals") is None


def test_yaml_records_bad_shape_raises(tmp_path):
    from weekly_summary import _yaml_records

    path = tmp_path / "pipeline.yaml"
    path.write_text("- just a list\n", encoding="utf-8")
    try:
        _yaml_records(path, "deals")
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-dict YAML")


# ── R3-11 week-scoped tasks_completed ───────────────────────────


def test_tasks_completed_uses_completed_at_when_present(tmp_path):
    from weekly_summary import build_weekly_summary_from_config

    root = tmp_path / "proj"
    root.mkdir()
    today = _today()
    last_week = today - timedelta(days=8)
    _write_yaml(root / "todos.yaml", {"todos": [
        {"id": "t-old", "status": "done", "completed_at": _iso(last_week)},
        {"id": "t-new", "status": "done", "completed_at": _iso(today)},
        {"id": "t-open", "status": "open", "due": _iso(today - timedelta(days=3))},
    ]})
    out = build_weekly_summary_from_config(cfg(root))
    assert out["tasks"]["tasks_completed"] == 1
    assert out["tasks"]["tasks_done_total"] == 2
    assert out["tasks"]["tasks_overdue_open"] == 1
    from briefing_renderer import render_weekly_text
    text = render_weekly_text(out)
    assert "task(s) completed" in text
    assert "open overdue" in text.lower()


# ── R3-12 bookkeeper outstanding populated ──────────────────────


def test_collect_bookkeeper_stats_outstanding_from_invoices(tmp_path):
    from briefing_sources import collect_bookkeeper_stats

    root = tmp_path / "proj"
    root.mkdir()
    today = _today()
    _write_yaml(root / "invoices.yaml", {"invoices": [
        {"id": "ar1", "direction": "sent", "status": "sent", "amount": 1200,
         "due_date": _iso(today - timedelta(days=2))},
        {"id": "ap1", "direction": "received", "status": "received", "amount": 150.50,
         "due_date": _iso(today + timedelta(days=10))},
        {"id": "paid", "direction": "sent", "status": "paid", "amount": 9,
         "due_date": _iso(today - timedelta(days=2))},
        {"id": "draft", "direction": "sent", "status": "draft", "amount": 8,
         "due_date": _iso(today - timedelta(days=2))},
    ]})
    stats = collect_bookkeeper_stats(cfg(root))
    assert stats["outstanding_ar"] == {"SGD": 1208}
    assert stats["outstanding_ap"] == {"SGD": 150.5}
    assert stats["overdue_count"] == 2


# ── R3-13 notify email missing-config banner ────────────────────


def test_cmd_notify_email_missing_config_prints_banner(tmp_path, monkeypatch, capsys):
    import daily_briefing as db

    monkeypatch.setattr(db, "_build_structured_briefing", lambda *a, **k: {"generated_at": ""})
    monkeypatch.setattr(db, "_attach_trends", lambda *a, **k: None)
    args = SimpleNamespace(
        config=str(tmp_path / "missing.yaml"),
        input=None,
        since=24,
        limit=20,
        dry_run=False,
        channel="email",
        to="ops@example.com",
        format=None,
    )
    rc = db.cmd_notify(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot load config" in err
    assert "Create it from" in err
    assert "--config" in err


# ── R3-14 empty trend store is ok ───────────────────────────────


def test_doctor_trend_snapshots_ok_when_never_written(tmp_path):
    from trend_history import doctor_snapshot_health, snapshot_health

    root = tmp_path / "proj"
    root.mkdir()
    config = cfg(root)
    assert snapshot_health(config)["status"] == "warn"
    health = doctor_snapshot_health(config)
    assert health["status"] == "ok"
    assert "not enabled" in health["detail"]


# ── R3-15 quiet load_config on directory arg ────────────────────


def test_build_weekly_summary_directory_arg_is_quiet(tmp_path, capsys):
    from weekly_summary import build_weekly_summary

    proj = tmp_path / "proj"
    proj.mkdir()
    result = build_weekly_summary(str(proj))
    assert isinstance(result, dict)
    err = capsys.readouterr().err
    assert str(proj) not in err


# ── R3-16 trend CSS gated on rendered html ──────────────────────


def test_unrenderable_trends_omits_section_and_css():
    from briefing_renderer import render_html

    html = render_html({
        "operator": "Tester",
        "summary": {"needs_attention": 0, "pending_approvals": 0, "suggestions": 0,
                    "classified_emails": 0, "system_warnings": 0},
        "sections": {},
        "trends": {"metrics": "not-a-list"},
    })
    assert "Trends" not in html
    assert ".trend-row" not in html


# ── R3-17 task statuses from canonical set ──────────────────────


def test_task_status_sets_track_todo_statuses():
    from schemas import TODO_STATUSES
    from weekly_summary import _DONE_STATUSES, _OPEN_STATUSES

    assert _DONE_STATUSES <= TODO_STATUSES
    assert _OPEN_STATUSES <= TODO_STATUSES
    assert not (_DONE_STATUSES & _OPEN_STATUSES)


# ── R3-18 weekly markdown branch ────────────────────────────────


def test_render_markdown_weekly_matches_weekly_text():
    from briefing_renderer import render, render_weekly_text

    briefing = {
        "kind": "weekly",
        "generated_at": "2026-09-20T01:00:00+00:00",
        "week": {"start": "2026-09-14", "end": "2026-09-20"},
        "operator": "Tester",
        "summary": {"deals_moved": 1, "invoices_sent": 0, "invoices_paid": 0,
                    "overdue_invoices": 0, "tasks_completed": 2,
                    "tasks_carry_over": 0, "wiki_pages_changed": 0},
        "pipeline": {"deals_by_stage": {"Proposal": 1}, "total_deals": 1, "deals_moved": 1},
        "bookkeeping": {},
        "tasks": {"tasks_completed": 2, "tasks_carry_over": 0, "tasks_overdue_open": 0},
        "knowledge": {},
        "expenses": {},
    }
    md = render(briefing, "markdown")
    assert md == render_weekly_text(briefing)
    assert "Good morning" not in md
    assert "Proposal" in md
